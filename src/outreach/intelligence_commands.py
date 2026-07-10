from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from outreach.artifacts import artifact_timestamp, write_artifact
from outreach.cadence import (
    build_workbook_cadence_plan,
    guard_cadence_action,
    summarize_cadence_plan,
)
from outreach.company_watchlist import (
    CandidateCompanySignal,
    build_candidate_review_queue,
    build_company_watchlist,
    company_discovery_summary,
    load_company_review_decisions,
    write_company_discovery_artifacts,
)
from outreach.config import OutreachSettings
from outreach.email_delivery import (
    EmailDeliveryConfig,
    EmailDeliveryResult,
    SmtpEmailSender,
    deliver_email_drafts,
)
from outreach.linkedin_signals import (
    CaptureLimits,
    FeedReviewDisposition,
    FeedSignalStore,
    capture_linkedin_signals_live,
)
from outreach.outcome_learning import (
    build_workbook_outcome_learning,
    concise_learning_summary,
    write_outcome_learning_artifact,
)
from outreach.recruiting_intelligence import (
    company_signals_from_feed_ledger,
    company_signals_from_source_metrics,
    role_inputs_from_source_metrics,
)
from outreach.role_surface_monitor import build_role_surface_report, write_role_surface_artifacts
from outreach.style_profile import sync_comms_learning_into_style_profile
from outreach.tracking import (
    ContactRecord,
    OrganizationRecord,
    OrganizationType,
    OutreachChannel,
    OutreachWorkbook,
    SourceKind,
    TouchpointRecord,
    utc_now_iso,
)


def register_intelligence_commands(app: typer.Typer) -> None:
    app.command("capture-linkedin-intelligence")(capture_linkedin_intelligence_cmd)
    app.command("review-linkedin-feed-signal")(review_linkedin_feed_signal_cmd)
    app.command("build-company-discovery-review")(build_company_discovery_review_cmd)
    app.command("build-role-surface-report")(build_role_surface_report_cmd)
    app.command("build-outreach-cadence-report")(build_outreach_cadence_report_cmd)
    app.command("build-outcome-learning-report")(build_outcome_learning_report_cmd)
    app.command("send-track-2-emails")(send_track_2_emails_cmd)


def capture_linkedin_intelligence_cmd(
    workspace: Annotated[Path, typer.Option(help="Outreach workspace directory")] = Path("workspace"),
    max_scrolls: Annotated[int, typer.Option(help="Maximum home-feed scrolls")] = 5,
    max_items: Annotated[int, typer.Option(help="Maximum feed items to inspect")] = 100,
    max_duration_seconds: Annotated[float, typer.Option(help="Optional time budget; 0 means no time cap")] = 0,
    profile_viewers_every_days: Annotated[int, typer.Option(help="Capture viewers when the passive ledger is this many days old; 0 captures every run")] = 7,
) -> None:
    """Capture read-only LinkedIn feed discovery and passive profile-view context."""
    settings = OutreachSettings()
    workbook = OutreachWorkbook(workspace)
    organizations = workbook.list_organizations()
    company_names = [item.name for item in organizations]
    viewer_path = workspace / "linkedin_profile_viewers.csv"
    capture_viewers = _capture_due(viewer_path, profile_viewers_every_days)
    limits = CaptureLimits(
        max_scrolls=max_scrolls,
        max_duration_seconds=max_duration_seconds or None,
        max_items=max_items,
        initial_wait_ms=2_500,
    )
    summary = capture_linkedin_signals_live(
        settings,
        feed_path=workspace / "linkedin_feed_signals.csv",
        profile_viewers_path=viewer_path,
        feed_limits=limits,
        capture_profile_viewers_this_run=capture_viewers,
        known_companies=company_names,
        target_companies=company_names,
    )
    artifact = write_artifact(settings.artifacts_dir, "linkedin-intelligence-capture", summary)
    typer.echo(f"LinkedIn feed: {summary.get('feed', {}).get('status', 'unknown')}")
    typer.echo(f"Profile viewers: {summary.get('profile_viewers', {}).get('status', 'unknown')}")
    typer.echo(f"Artifact: {artifact}")
    if str(summary.get("status") or "failed") != "completed":
        raise typer.Exit(code=1)


def review_linkedin_feed_signal_cmd(
    signal_id: Annotated[str, typer.Argument(help="Feed signal id")],
    disposition: Annotated[FeedReviewDisposition, typer.Option(help="Manual review disposition")],
    note: Annotated[str, typer.Option(help="Optional review note")] = "",
    workspace: Annotated[Path, typer.Option(help="Outreach workspace directory")] = Path("workspace"),
) -> None:
    row = FeedSignalStore(workspace / "linkedin_feed_signals.csv").review(
        signal_id,
        disposition,
        note=note,
    )
    typer.echo(f"Reviewed {signal_id}: {row['review_disposition']}")


def build_company_discovery_review_cmd(
    workspace: Annotated[Path, typer.Option(help="Outreach workspace directory")] = Path("workspace"),
    run_id: Annotated[str, typer.Option(help="Run id for provenance")] = "",
    capture_artifact: Annotated[
        Path | None,
        typer.Option(
            help="Exact LinkedIn capture artifact for same-run discovery scope; omit for a manual workspace rebuild"
        ),
    ] = None,
    source_metrics: Annotated[
        Path | None,
        typer.Option(help="Exact ResumeGenerator source-metrics artifact for same-run startup discovery"),
    ] = None,
    promote_approved: Annotated[bool, typer.Option(help="Write human-approved, rubric-qualified watchlist entries into organizations.csv")] = False,
) -> None:
    settings = OutreachSettings()
    run_id = run_id or artifact_timestamp()
    feed_path = workspace / "linkedin_feed_signals.csv"
    output_dir = workspace / "company_discovery"
    review_path = output_dir / "company_discovery_review.csv"
    known_companies = [item.name for item in OutreachWorkbook(workspace).list_organizations()]
    historical_signals = _load_historical_company_signals(
        output_dir / "company_discovery_candidates.json",
        known_companies=known_companies,
    )
    feed_ledger_signals = company_signals_from_feed_ledger(
        feed_path,
        run_id="workspace-ledger",
        known_companies=known_companies,
    )
    source_signals = (
        company_signals_from_source_metrics(
            source_metrics,
            run_id=run_id,
            known_companies=known_companies,
        )
        if source_metrics is not None
        else []
    )
    capture_payload = _load_json(capture_artifact) if capture_artifact else {}
    feed_capture = (
        capture_payload.get("feed")
        if isinstance(capture_payload.get("feed"), dict)
        else {}
    )
    if capture_artifact is not None:
        captured_ids_value = feed_capture.get("captured_signal_ids")
        captured_ids = (
            [str(item) for item in captured_ids_value]
            if isinstance(captured_ids_value, list)
            else None
        )
        observed_at = str(
            feed_capture.get("observed_at") or capture_payload.get("observed_at") or ""
        )
        if captured_ids is None and not observed_at:
            captured_ids = []
        run_signals = company_signals_from_feed_ledger(
            feed_path,
            run_id=run_id,
            known_companies=known_companies,
            signal_ids=captured_ids,
            observed_at=observed_at if captured_ids is None else "",
        )
        capture_status = str(feed_capture.get("status") or "failed")
    else:
        run_signals = list(feed_ledger_signals) if source_metrics is None else []
        capture_status = "not_scheduled" if source_metrics is not None else "manual_workspace_rebuild"
    run_signals.extend(source_signals)
    all_signals = [*historical_signals, *feed_ledger_signals, *source_signals]
    reviews = load_company_review_decisions(review_path)
    artifacts = write_company_discovery_artifacts(
        output_dir,
        run_id=run_id,
        signals=all_signals,
        review_decisions=reviews,
    )
    workspace_summary = json.loads(artifacts.summary_json.read_text(encoding="utf-8"))
    run_candidates = build_candidate_review_queue(run_signals, review_decisions=reviews)
    run_watchlist = build_company_watchlist(run_candidates)
    run_summary = company_discovery_summary(run_signals, run_candidates, run_watchlist)
    run_summary["capture_status"] = capture_status
    scope_parts: list[str] = []
    if capture_artifact is not None:
        scope_parts.append("same LinkedIn capture artifact")
    if source_metrics is not None:
        scope_parts.append("same-run startup source metrics")
    run_summary["scope"] = " + ".join(scope_parts) or "manual workspace rebuild"
    promoted = _promote_approved_watchlist(workspace, artifacts.watchlist_json) if promote_approved else 0
    payload = {
        "run_id": run_id,
        "source": str(feed_path),
        "capture_artifact": str(capture_artifact or ""),
        "source_metrics": str(source_metrics or ""),
        "summary": run_summary,
        "workspace_summary": workspace_summary,
        "promote_approved": promote_approved,
        "organizations_promoted": promoted,
        "artifacts": {key: str(value) for key, value in artifacts.__dict__.items()},
    }
    artifact = write_artifact(settings.artifacts_dir, "company-discovery-review", payload)
    typer.echo(f"Company signals this run: {run_summary.get('signals_received', 0)}")
    typer.echo(f"Pending review in workspace: {workspace_summary.get('pending_review', 0)}")
    typer.echo(f"Approved/promoted: {workspace_summary.get('promoted_to_watchlist', 0)}/{promoted}")
    typer.echo(f"Review CSV: {artifacts.review_queue_csv}")
    typer.echo(f"Artifact: {artifact}")


def _load_json(path: Path | None) -> dict[str, object]:
    if path is None or not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_historical_company_signals(
    path: Path,
    *,
    known_companies: list[str],
) -> list[CandidateCompanySignal]:
    payload = _load_json(path)
    rows = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
    known = {item.strip().casefold() for item in known_companies if item.strip()}
    signals: list[CandidateCompanySignal] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        provenance = row.get("provenance")
        if isinstance(provenance, list) and provenance and all(
            isinstance(item, dict)
            and str(item.get("source_type") or "") == "linkedin_home_feed"
            for item in provenance
        ):
            continue
        if str(row.get("company_name") or "").strip().casefold() in known:
            continue
        try:
            signals.append(
                CandidateCompanySignal.model_validate(
                    {
                        key: row.get(key)
                        for key in (
                            "company_name",
                            "website",
                            "linkedin_company_url",
                            "description",
                            "rubric",
                            "provenance",
                        )
                    }
                )
            )
        except ValueError:
            continue
    return signals


def build_role_surface_report_cmd(
    source_metrics: Annotated[Path, typer.Option(help="Run-scoped ResumeGenerator source metrics JSON")],
    run_id: Annotated[str, typer.Option(help="Run id; defaults to source metrics stem")] = "",
    workspace: Annotated[Path, typer.Option(help="Outreach workspace directory")] = Path("workspace"),
) -> None:
    settings = OutreachSettings()
    run_id = run_id or source_metrics.stem
    observations, source_runs = role_inputs_from_source_metrics(source_metrics, run_id=run_id)
    report = build_role_surface_report(
        run_id=run_id,
        observations=observations,
        source_runs=source_runs,
    )
    artifacts = write_role_surface_artifacts(workspace / "role_surface", report)
    payload = report.model_dump(mode="json")
    payload["artifacts"] = {key: str(value) for key, value in artifacts.__dict__.items()}
    artifact = write_artifact(settings.artifacts_dir, "role-surface-report", payload)
    typer.echo(report.summary_text)
    typer.echo(f"Artifact: {artifact}")


def build_outreach_cadence_report_cmd(
    workspace: Annotated[Path, typer.Option(help="Outreach workspace directory")] = Path("workspace"),
) -> None:
    settings = OutreachSettings()
    plan = build_workbook_cadence_plan(OutreachWorkbook(workspace))
    summary = summarize_cadence_plan(plan)
    payload = {"created_at": utc_now_iso(), "summary": summary, "results": [item.as_dict() for item in plan]}
    artifact = write_artifact(settings.artifacts_dir, "outreach-cadence-report", payload)
    latest = workspace / "outreach_cadence_plan.json"
    latest.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    typer.echo(f"Cadence decisions: {summary['total']}")
    typer.echo(f"Due: {len(summary['due'])}; suppressed: {len(summary['suppressed'])}")
    typer.echo(f"Artifact: {artifact}")


def build_outcome_learning_report_cmd(
    workspace: Annotated[Path, typer.Option(help="Outreach workspace directory")] = Path("workspace"),
) -> None:
    settings = OutreachSettings()
    corpus_path = workspace / "comms_learning" / "linkedin_examples.jsonl"
    workbook = OutreachWorkbook(workspace)
    style_sync = sync_comms_learning_into_style_profile(
        profile_path=workspace / "communication_style_profile.yml",
        examples_path=corpus_path,
        contacts=workbook.list_contacts(),
        organizations=workbook.list_organizations(),
    )
    report = build_workbook_outcome_learning(
        workbook,
        labeled_examples_path=corpus_path if corpus_path.exists() else None,
    )
    latest = write_outcome_learning_artifact(workspace / "comms_learning" / "outcome_learning.json", report)
    summary = concise_learning_summary(report)
    summary["style_profile_sync"] = style_sync.as_dict()
    artifact = write_artifact(
        settings.artifacts_dir,
        "outcome-learning-report",
        {"summary": summary, "report": report.as_dict(), "latest": str(latest)},
    )
    typer.echo(f"Outcome totals: {summary['totals']}")
    typer.echo(f"Recommendations: {len(summary['recommendations'])}")
    typer.echo(f"Artifact: {artifact}")


def send_track_2_emails_cmd(
    draft_artifact: Annotated[Path, typer.Option(help="Track 2 email draft/review artifact")],
    approval_csv: Annotated[
        Path | None,
        typer.Option(help="Marked communication review CSV whose approved rows authorize this batch"),
    ] = None,
    workspace: Annotated[Path, typer.Option(help="Outreach workspace directory")] = Path("workspace"),
    limit: Annotated[int, typer.Option(help="Maximum emails in this bounded batch")] = 5,
    execute: Annotated[bool, typer.Option(help="Send through configured SMTP and record successful touchpoints")] = False,
) -> None:
    settings = OutreachSettings()
    payload = json.loads(draft_artifact.read_text(encoding="utf-8"))
    drafts = [item for item in list(payload.get("results") or []) if isinstance(item, dict)]
    approvals = _load_email_approvals(approval_csv, draft_artifact=draft_artifact)
    workbook = OutreachWorkbook(workspace)
    touchpoints = workbook.list_touchpoints()
    touchpoint_by_id = {item.touchpoint_id: item for item in touchpoints}
    contacts = workbook.list_contacts()
    contact_by_id = {item.contact_id: item for item in contacts}
    cadence = build_workbook_cadence_plan(workbook)
    recommendation_by_contact = {
        item.contact_id: item for item in cadence if item.channel == "email"
    }
    eligible: list[dict[str, object]] = []
    held: list[dict[str, object]] = []
    seen_contacts: set[str] = set()
    for original_draft in drafts:
        draft = _apply_email_approval(original_draft, approvals)
        contact_id = str(draft.get("contact_id") or "")
        organization_id = str(draft.get("organization_id") or "")
        if contact_id in seen_contacts:
            held.append(
                {
                    **draft,
                    "delivery_status": "duplicate_contact",
                    "delivery_detail": "only one email per contact is allowed in a batch",
                }
            )
            continue
        seen_contacts.add(contact_id)
        contact = contact_by_id.get(contact_id)
        if contact is None or not _email_is_verified(contact, draft):
            held.append(
                {
                    **draft,
                    "delivery_status": "email_unverified",
                    "delivery_detail": (
                        "email must come from verified research or match the address in the human approval CSV"
                    ),
                }
            )
            continue
        recommendation = recommendation_by_contact.get(contact_id)
        if recommendation is None:
            held.append({**draft, "delivery_status": "cadence_blocked", "delivery_detail": "no tracker-backed email cadence decision"})
            continue
        draft_action = str(draft.get("cadence_action") or "").strip()
        if draft_action != recommendation.action:
            held.append(
                {
                    **draft,
                    "delivery_status": "cadence_mismatch",
                    "delivery_detail": (
                        f"draft was built for {draft_action or 'no cadence action'}; "
                        f"tracker now requires {recommendation.action}"
                    ),
                }
            )
            continue
        guard = guard_cadence_action(
            touchpoints,
            organization_id=organization_id,
            contact_id=contact_id,
            channel="email",
            action=recommendation.action,
            proposed_message=str(draft.get("body") or ""),
            contacts=contacts,
        )
        if not guard.allowed:
            held.append({**draft, "delivery_status": "cadence_blocked", "delivery_detail": "; ".join(guard.reasons)})
            continue
        if not _email_is_approved(draft):
            held.append({**draft, "delivery_status": "needs_review", "delivery_detail": "explicit approval marker required"})
            continue
        attempt_scope = _email_delivery_attempt_scope(
            draft_artifact=draft_artifact,
            cadence_action=recommendation.action,
            contact_id=contact_id,
            touchpoints=touchpoints,
        )
        attempt_id = workbook.make_touchpoint_id(
            organization_id,
            contact_id,
            OutreachChannel.EMAIL.value,
            str(draft.get("body") or ""),
            attempt_scope,
        )
        prior_attempt = touchpoint_by_id.get(attempt_id)
        if prior_attempt is not None and prior_attempt.status.strip().lower() == "sending":
            held.append(
                {
                    **draft,
                    "delivery_status": "delivery_uncertain",
                    "delivery_detail": (
                        "a prior SMTP attempt is still marked Sending; reconcile it before retrying"
                    ),
                }
            )
            continue
        eligible.append(
            {
                **draft,
                "cadence_action": recommendation.action,
                "delivery_attempt_id": attempt_id,
                "delivery_attempt_scope": attempt_scope,
            }
        )

    sender = SmtpEmailSender(EmailDeliveryConfig.from_env()) if execute and eligible else None

    def before_send(item: dict[str, object]) -> None:
        attempt_id = str(item.get("delivery_attempt_id") or "")
        if touchpoint_by_id.get(attempt_id) is not None:
            workbook.update_touchpoint(
                attempt_id,
                status="Sending",
                recorded_at=utc_now_iso(),
                sent_at="",
                notes="smtp_delivery=attempting",
            )
            return
        record = TouchpointRecord(
            touchpoint_id=attempt_id,
            organization_id=str(item.get("organization_id") or ""),
            contact_id=str(item.get("contact_id") or ""),
            channel=OutreachChannel.EMAIL,
            status="Sending",
            message_kind=str(item.get("cadence_action") or "cold_email"),
            message_text=str(item.get("body") or ""),
            source_artifact=str(draft_artifact.resolve()),
            notes=(
                "smtp_delivery=attempting;"
                f"attempt_scope={item.get('delivery_attempt_scope') or ''}"
            ),
        )
        workbook.append_touchpoint(record)
        touchpoint_by_id[attempt_id] = record

    def after_send(item: dict[str, object], outcome: EmailDeliveryResult) -> None:
        sent = outcome.status == "sent"
        workbook.update_touchpoint(
            str(item.get("delivery_attempt_id") or ""),
            status="Sent" if sent else "Failed",
            sent_at=utc_now_iso() if sent else "",
            notes=(
                "smtp_delivery=sent"
                if sent
                else f"smtp_delivery=failed;detail={outcome.detail[:240]}"
            ),
        )

    delivered = deliver_email_drafts(
        eligible,
        sender=sender,
        execute=execute,
        limit=limit,
        before_send=before_send if execute else None,
        after_send=after_send if execute else None,
    )
    sent_count = sum(item.get("delivery_status") == "sent" for item in delivered)
    result_payload = {
        "source_artifact": str(draft_artifact),
        "execute": execute,
        "eligible": len(eligible),
        "held": len(held),
        "sent": sent_count,
        "results": delivered + held,
    }
    artifact = write_artifact(settings.artifacts_dir, "track-2-email-send-results", result_payload)
    typer.echo(f"Eligible: {len(eligible)}; held: {len(held)}; sent: {sent_count}")
    typer.echo(f"Artifact: {artifact}")


def _capture_due(path: Path, every_days: int) -> bool:
    if every_days <= 0 or not path.exists():
        return True
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    observed: list[datetime] = []
    for row in rows:
        value = str(row.get("last_seen_at") or "").strip().replace("Z", "+00:00")
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            continue
        observed.append(parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC))
    if not observed:
        return True
    return datetime.now(UTC) - max(observed) >= timedelta(days=every_days)


def _promote_approved_watchlist(workspace: Path, path: Path) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = [item for item in list(payload.get("entries") or []) if isinstance(item, dict)]
    workbook = OutreachWorkbook(workspace)
    added = 0
    for item in entries:
        provenance = list(item.get("provenance") or [])
        source_url = str((provenance[0] if provenance else {}).get("source_url") or item.get("linkedin_company_url") or item.get("website") or "")
        notes = (
            "Human-approved company discovery watchlist | "
            f"rubric_total={item.get('rubric_total', 0)} | "
            f"reviewer_notes={item.get('reviewer_notes', '')}"
        )
        _, created = workbook.upsert_organization(
            OrganizationRecord(
                organization_id=workbook.make_organization_id(str(item.get("company_name") or "")),
                name=str(item.get("company_name") or ""),
                organization_type=OrganizationType.COMPANY,
                target_lists="company-watchlist;track-2;relationship",
                status="Reviewed watchlist",
                website=str(item.get("website") or ""),
                source_kind=SourceKind.LINKEDIN,
                source_url=source_url,
                notes=notes,
            )
        )
        added += int(created)
    return added


def _email_is_approved(draft: dict[str, object]) -> bool:
    decision = str(draft.get("user_decision") or "").strip().lower()
    return (
        draft.get("approval_binding_valid") is True
        and draft.get("approval_email_matches") is True
        and decision in {
            "approved",
            "approve",
            "send",
            "safe_to_send",
        }
    )


def _email_delivery_attempt_scope(
    *,
    draft_artifact: Path,
    cadence_action: str,
    contact_id: str,
    touchpoints: list[TouchpointRecord],
) -> str:
    """Identify one retry-safe send attempt within a cadence episode."""

    sent_email_touchpoints = [
        item
        for item in touchpoints
        if item.contact_id == contact_id
        and item.channel == OutreachChannel.EMAIL
        and item.status.strip().casefold() == "sent"
    ]
    latest_sent = (
        max(
            sent_email_touchpoints,
            key=lambda item: _touchpoint_event_at(item),
        )
        if sent_email_touchpoints
        else None
    )
    previous_send_id = latest_sent.touchpoint_id if latest_sent is not None else "no-prior-send"
    return (
        f"{draft_artifact.resolve()}#cadence={cadence_action};"
        f"after={previous_send_id}"
    )


def _touchpoint_event_at(item: TouchpointRecord) -> datetime:
    value = str(item.sent_at or item.recorded_at or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _email_is_verified(contact: ContactRecord, draft: dict[str, object]) -> bool:
    tracker_email = contact.email.strip().casefold()
    draft_email = str(draft.get("email") or "").strip().casefold()
    if not tracker_email or tracker_email != draft_email:
        return False
    notes = contact.notes.casefold()
    if any(
        marker in notes
        for marker in (
            "linkedin_contact_info_email_found=",
            "external_email_found=",
            "email_verified=true",
        )
    ):
        return True
    explicit = str(draft.get("email_verification_status") or "").strip().casefold()
    if explicit in {"verified", "valid", "accept_all", "human_verified"}:
        return True
    return draft.get("approval_email_matches") is True


def _load_email_approvals(
    path: Path | None,
    *,
    draft_artifact: Path,
) -> dict[tuple[str, str, str, str, str], dict[str, str]]:
    if path is None:
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    result: dict[tuple[str, str, str, str, str], dict[str, str]] = {}
    expected_artifact = draft_artifact.resolve()
    for row in rows:
        review_artifact = str(row.get("review_artifact") or "").strip()
        if not review_artifact or Path(review_artifact).resolve() != expected_artifact:
            continue
        key = (
            str(row.get("organization_id") or "").strip(),
            str(row.get("contact_id") or "").strip(),
            str(row.get("email") or "").strip().casefold(),
            str(row.get("subject") or "").strip(),
            str(row.get("message") or "").strip(),
        )
        if all(key):
            result[key] = row
    return result


def _apply_email_approval(
    draft: dict[str, object],
    approvals: dict[tuple[str, str, str, str, str], dict[str, str]],
) -> dict[str, object]:
    key = (
        str(draft.get("organization_id") or "").strip(),
        str(draft.get("contact_id") or "").strip(),
        str(draft.get("email") or "").strip().casefold(),
        str(draft.get("subject") or "").strip(),
        str(draft.get("body") or "").strip(),
    )
    approval = approvals.get(key)
    if approval is None:
        return dict(draft)
    result = {
        **draft,
        "user_decision": str(approval.get("user_decision") or "").strip(),
        "user_reason": str(approval.get("user_reason") or "").strip(),
        "approval_source": str(approval.get("review_artifact") or "review_csv"),
        "approval_binding_valid": True,
        "approval_email_matches": (
            bool(str(approval.get("email") or "").strip())
            and str(approval.get("email") or "").strip().casefold()
            == str(draft.get("email") or "").strip().casefold()
        ),
    }
    user_edit = str(approval.get("user_edit") or "").strip()
    if user_edit:
        result["body"] = user_edit
        result["body_length"] = len(user_edit)
        result["user_edit_applied"] = True
    return result

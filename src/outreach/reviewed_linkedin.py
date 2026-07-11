from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

from outreach.invite_reservations import atomic_write_json


SCHEMA_VERSION = 1
APPROVAL_KIND = "reviewed_linkedin_approval"
LEDGER_KIND = "reviewed_linkedin_execution_ledger"
SUPPORTED_ACTIONS = {"invite", "followup"}


class ReplayProtectedError(ValueError):
    """Raised when an approved send has already crossed the execution boundary."""


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def payload_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_linkedin_profile(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    host = (parts.hostname or "").casefold()
    path = parts.path.rstrip("/")
    if host not in {"linkedin.com", "www.linkedin.com"} and not host.endswith(".linkedin.com"):
        return ""
    if not path.casefold().startswith(("/in/", "/pub/")):
        return ""
    return urlunsplit(("https", "www.linkedin.com", path, "", ""))


def canonical_linkedin_thread(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    host = (parts.hostname or "").casefold()
    path = parts.path.rstrip("/")
    if host not in {"linkedin.com", "www.linkedin.com"} and not host.endswith(".linkedin.com"):
        return ""
    if not path.casefold().startswith("/messaging/thread/"):
        return ""
    return urlunsplit(("https", "www.linkedin.com", path, "", ""))


def configured_workspace_root() -> Path:
    """Return the one configured tracker root that owns replay state."""

    from outreach.config import OutreachSettings

    return OutreachSettings().resolved_tracking_workspace_dir.expanduser().resolve()


def canonical_review_state_root() -> Path:
    return configured_workspace_root() / ".reviewed_linkedin"


def canonical_execution_ledger_path() -> Path:
    return canonical_review_state_root() / "execution-ledger.json"


def _read_source(
    source_artifact: Path,
    row_index: int,
) -> tuple[Path, dict, dict, str]:
    path = Path(source_artifact).expanduser().resolve(strict=True)
    try:
        source_bytes = path.read_bytes()
        payload = json.loads(source_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"LinkedIn source artifact is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("LinkedIn source artifact must contain a JSON object")
    rows = payload.get("results")
    if not isinstance(rows, list):
        raise ValueError("LinkedIn source artifact must contain a results array")
    if row_index < 0 or row_index >= len(rows):
        raise ValueError(f"LinkedIn source row index is out of range: {row_index}")
    row = rows[row_index]
    if not isinstance(row, dict):
        raise ValueError(f"LinkedIn source row {row_index} is not an object")
    return path, payload, row, hashlib.sha256(source_bytes).hexdigest()


def _company_for_row(payload: Mapping[str, object], row: Mapping[str, object]) -> str:
    payload_company = str(payload.get("company") or "").strip()
    row_company = str(row.get("company") or "").strip()
    if payload_company and row_company and payload_company.casefold() != row_company.casefold():
        raise ValueError(
            "LinkedIn source row company conflicts with the artifact company: "
            f"{row_company!r} != {payload_company!r}"
        )
    company = row_company or payload_company
    if not company:
        raise ValueError("Reviewed LinkedIn send requires an exact company")
    return company


def _message_window(row: Mapping[str, object]) -> list[dict[str, object]]:
    raw = row.get("message_window")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _latest_inbound_context(row: Mapping[str, object]) -> dict[str, object]:
    window = _message_window(row)
    observed_message = str(row.get("latest_message") or "").strip()
    observed_sender = str(row.get("last_sender") or "").strip()
    observed_timestamp = str(row.get("timestamp_text") or "").strip()
    inbound = [
        item
        for item in window
        if str(item.get("sender") or "").strip().casefold() not in {"", "you", "me"}
    ]
    latest_inbound = None
    if observed_message and observed_sender.casefold() not in {"", "you", "me"}:
        latest_inbound = next(
            (
                dict(item)
                for item in inbound
                if str(item.get("sender") or "").strip().casefold() == observed_sender.casefold()
                and str(item.get("message") or "").strip() == observed_message
            ),
            {
                "sender": observed_sender,
                "message": observed_message,
                "timestamp_text": observed_timestamp,
                "source": "observed_latest",
            },
        )
    elif inbound:
        latest_inbound = dict(inbound[0])
    return {
        "latest_inbound": latest_inbound,
        "observed_latest_message": observed_message,
        "observed_last_sender": observed_sender,
        "observed_timestamp_text": observed_timestamp,
        "message_window": window,
    }


def _recipient_for_row(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "name": str(row.get("name") or row.get("full_name") or "").strip(),
        "linkedin_profile": canonical_linkedin_profile(str(row.get("linkedin_url") or "")),
        "contact_id": str(row.get("contact_id") or "").strip(),
        "organization_id": str(row.get("organization_id") or "").strip(),
        "thread_id": str(row.get("thread_id") or "").strip(),
        "thread_url": canonical_linkedin_thread(str(row.get("thread_url") or "")),
    }


def _is_exact_thread_id(value: object) -> bool:
    thread_id = str(value or "").strip()
    return bool(thread_id) and not thread_id.casefold().startswith("synthetic:")


def _json_snapshot(value: object) -> object:
    """Detach a JSON value from mutable caller/source objects."""

    return json.loads(canonical_json_bytes(value))


def build_review_proposal(
    *,
    action: str,
    source_artifact: Path,
    row_index: int,
    outgoing_message: str | None = None,
) -> dict[str, object]:
    normalized_action = str(action or "").strip().casefold()
    if normalized_action not in SUPPORTED_ACTIONS:
        raise ValueError(f"Unsupported reviewed LinkedIn action: {action!r}")
    path, payload, row, source_sha256 = _read_source(source_artifact, row_index)
    company = _company_for_row(payload, row)
    recipient = _recipient_for_row(row)
    name = str(recipient["name"])
    if not name:
        raise ValueError("Reviewed LinkedIn send requires an exact recipient name")

    profile = str(recipient["linkedin_profile"])
    source_message_field = "note" if normalized_action == "invite" else "draft_message"
    message = (
        str(outgoing_message).strip()
        if outgoing_message is not None
        else str(row.get(source_message_field) or "").strip()
    )
    if not message:
        raise ValueError("Reviewed LinkedIn send requires a non-empty outgoing message")

    if normalized_action == "invite" and not profile:
        raise ValueError("Reviewed LinkedIn invite requires a canonical LinkedIn profile URL")
    if normalized_action == "followup" and not _is_exact_thread_id(recipient["thread_id"]):
        raise ValueError("Reviewed LinkedIn follow-up requires an exact non-synthetic thread_id")

    approved_row = _json_snapshot(row)
    assert isinstance(approved_row, dict)
    approved_row[source_message_field] = message
    execution_source_snapshot = _json_snapshot(payload)
    assert isinstance(execution_source_snapshot, dict)
    execution_source_snapshot["results"] = [approved_row]

    proposal: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "action": normalized_action,
        "source_artifact": str(path),
        "source_sha256": source_sha256,
        "source_row_index": row_index,
        "workspace_root": str(configured_workspace_root()),
        "recipient": recipient,
        "company": company,
        "latest_inbound_context": (
            _latest_inbound_context(row) if normalized_action == "followup" else None
        ),
        "outgoing_message": message,
        "approved_row_snapshot": approved_row,
        "execution_source_snapshot": execution_source_snapshot,
        "execution_source_snapshot_sha256": payload_sha256(execution_source_snapshot),
    }
    proposal["proposal_sha256"] = payload_sha256(proposal)
    return proposal


def _proposal_digest(proposal: Mapping[str, object]) -> str:
    unsigned = dict(proposal)
    claimed = str(unsigned.pop("proposal_sha256", ""))
    actual = payload_sha256(unsigned)
    if not claimed or claimed != actual:
        raise ValueError("Reviewed LinkedIn proposal SHA256 is invalid")
    return actual


def create_approval(
    *,
    proposal: Mapping[str, object],
    expected_proposal_sha256: str,
    approved_by: str,
    approved_at: str | None = None,
) -> dict[str, object]:
    proposal_digest = _proposal_digest(proposal)
    if proposal_digest != str(expected_proposal_sha256 or "").strip().casefold():
        raise ValueError("Reviewed LinkedIn proposal changed after human review")
    reviewer = str(approved_by or "").strip()
    if not reviewer:
        raise ValueError("Reviewed LinkedIn approval requires approved_by")
    unsigned: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "kind": APPROVAL_KIND,
        "proposal": dict(proposal),
        "proposal_sha256": proposal_digest,
        "approved_by": reviewer,
        "approved_at": approved_at or utc_now_iso(),
    }
    approval = dict(unsigned)
    approval["approval_sha256"] = payload_sha256(unsigned)
    return approval


def write_immutable_json(path: Path, payload: Mapping[str, object]) -> None:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def write_approval(
    path: Path,
    *,
    proposal: Mapping[str, object],
    expected_proposal_sha256: str,
    approved_by: str,
    approved_at: str | None = None,
) -> dict[str, object]:
    approval = create_approval(
        proposal=proposal,
        expected_proposal_sha256=expected_proposal_sha256,
        approved_by=approved_by,
        approved_at=approved_at,
    )
    write_immutable_json(path, approval)
    return approval


def _validate_proposal_snapshot(proposal: Mapping[str, object]) -> tuple[dict, dict]:
    action = str(proposal.get("action") or "")
    if action not in SUPPORTED_ACTIONS:
        raise ValueError("Reviewed LinkedIn approval has an unsupported action")
    expected_workspace = str(configured_workspace_root())
    if str(proposal.get("workspace_root") or "") != expected_workspace:
        raise ValueError("Reviewed LinkedIn approval belongs to a different configured workspace")
    source_sha = str(proposal.get("source_sha256") or "")
    if len(source_sha) != 64 or any(
        character not in "0123456789abcdef" for character in source_sha
    ):
        raise ValueError("Reviewed LinkedIn approval has an invalid source SHA256")

    approved_row = proposal.get("approved_row_snapshot")
    execution_source = proposal.get("execution_source_snapshot")
    if not isinstance(approved_row, dict) or not isinstance(execution_source, dict):
        raise ValueError("Reviewed LinkedIn approval is missing its immutable row snapshot")
    rows = execution_source.get("results")
    if not isinstance(rows, list) or rows != [approved_row]:
        raise ValueError(
            "Reviewed LinkedIn execution snapshot must contain exactly its approved row"
        )
    if payload_sha256(execution_source) != str(
        proposal.get("execution_source_snapshot_sha256") or ""
    ):
        raise ValueError("Reviewed LinkedIn execution snapshot SHA256 is invalid")

    company = _company_for_row(execution_source, approved_row)
    recipient = _recipient_for_row(approved_row)
    message_field = "note" if action == "invite" else "draft_message"
    outgoing_message = str(approved_row.get(message_field) or "").strip()
    if proposal.get("company") != company:
        raise ValueError("Reviewed LinkedIn company binding does not match its row snapshot")
    if proposal.get("recipient") != recipient:
        raise ValueError("Reviewed LinkedIn recipient binding does not match its row snapshot")
    if proposal.get("outgoing_message") != outgoing_message:
        raise ValueError("Reviewed LinkedIn message binding does not match its row snapshot")
    expected_context = _latest_inbound_context(approved_row) if action == "followup" else None
    if proposal.get("latest_inbound_context") != expected_context:
        raise ValueError("Reviewed LinkedIn context binding does not match its row snapshot")
    if action == "invite" and not recipient["linkedin_profile"]:
        raise ValueError("Reviewed LinkedIn invite snapshot has no canonical profile")
    if action == "followup" and not _is_exact_thread_id(recipient["thread_id"]):
        raise ValueError("Reviewed LinkedIn follow-up snapshot has no exact thread_id")
    detached_row = _json_snapshot(approved_row)
    detached_source = _json_snapshot(execution_source)
    assert isinstance(detached_row, dict) and isinstance(detached_source, dict)
    return detached_row, detached_source


def load_and_validate_approval(path: Path, *, expected_approval_sha256: str) -> dict[str, object]:
    approval_path = Path(path).expanduser().resolve(strict=True)
    try:
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Reviewed LinkedIn approval is not valid JSON: {approval_path}") from exc
    if not isinstance(approval, dict):
        raise ValueError("Reviewed LinkedIn approval must contain a JSON object")
    unsigned = dict(approval)
    claimed = str(unsigned.pop("approval_sha256", ""))
    actual = payload_sha256(unsigned)
    expected = str(expected_approval_sha256 or "").strip().casefold()
    if not claimed or claimed != actual or expected != actual:
        raise ValueError("Reviewed LinkedIn approval SHA256 is invalid or unexpected")
    if approval.get("schema_version") != SCHEMA_VERSION or approval.get("kind") != APPROVAL_KIND:
        raise ValueError("Reviewed LinkedIn approval has an unsupported schema")
    proposal = approval.get("proposal")
    if not isinstance(proposal, dict):
        raise ValueError("Reviewed LinkedIn approval is missing its proposal")
    if _proposal_digest(proposal) != str(approval.get("proposal_sha256") or ""):
        raise ValueError("Reviewed LinkedIn approval proposal binding is invalid")
    _validate_proposal_snapshot(proposal)
    return approval


def _empty_ledger() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": LEDGER_KIND,
        "updated_at": "",
        "executions": {},
    }


def _load_ledger(path: Path) -> dict[str, object]:
    if not path.exists():
        return _empty_ledger()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Reviewed LinkedIn execution ledger is unreadable: {path}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("kind") != LEDGER_KIND
        or not isinstance(payload.get("executions"), dict)
    ):
        raise ValueError(f"Reviewed LinkedIn execution ledger has an unsupported schema: {path}")
    return payload


def _with_locked_ledger(path: Path, update: Callable[[dict[str, object]], object]) -> object:
    ledger_path = Path(path).expanduser()
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger_path.with_suffix(ledger_path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        ledger = _load_ledger(ledger_path)
        result = update(ledger)
        atomic_write_json(ledger_path, ledger)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        return result


def reserve_approval_execution(
    ledger_path: Path,
    *,
    approval: Mapping[str, object],
    approval_path: Path,
    now: str | None = None,
) -> dict[str, object]:
    approval_digest = str(approval.get("approval_sha256") or "")
    timestamp = now or utc_now_iso()

    def update(ledger: dict[str, object]) -> dict[str, object]:
        executions = ledger["executions"]
        assert isinstance(executions, dict)
        existing = executions.get(approval_digest)
        if isinstance(existing, dict):
            raise ReplayProtectedError(
                "Reviewed LinkedIn approval was already consumed before execution: "
                f"status={existing.get('status') or 'unknown'}"
            )
        proposal_digest = str(approval.get("proposal_sha256") or "")
        prior_for_proposal = next(
            (
                record
                for record in executions.values()
                if isinstance(record, dict)
                and str(record.get("proposal_sha256") or "") == proposal_digest
            ),
            None,
        )
        if prior_for_proposal is not None:
            raise ReplayProtectedError(
                "Reviewed LinkedIn proposal was already consumed by another approval: "
                f"status={prior_for_proposal.get('status') or 'unknown'}"
            )
        proposal = approval.get("proposal")
        assert isinstance(proposal, dict)
        record: dict[str, object] = {
            "approval_sha256": approval_digest,
            "approval_file": str(Path(approval_path).expanduser().resolve()),
            "proposal_sha256": proposal_digest,
            "action": str(proposal.get("action") or ""),
            "status": "execution_reserved",
            "consumed_at": timestamp,
            "updated_at": timestamp,
            "detail": (
                "Approval consumed before entering any LinkedIn send implementation; "
                "automatic replay is blocked."
            ),
        }
        executions[approval_digest] = record
        ledger["updated_at"] = timestamp
        return dict(record)

    return _with_locked_ledger(ledger_path, update)  # type: ignore[return-value]


def finalize_approval_execution(
    ledger_path: Path,
    *,
    approval_sha256: str,
    status: str,
    detail: str,
    reconciliation_required: bool,
    receipt_file: Path,
    now: str | None = None,
) -> dict[str, object]:
    timestamp = now or utc_now_iso()

    def update(ledger: dict[str, object]) -> dict[str, object]:
        executions = ledger["executions"]
        assert isinstance(executions, dict)
        record = executions.get(approval_sha256)
        if not isinstance(record, dict):
            raise ValueError("Reviewed LinkedIn execution reservation disappeared")
        record.update(
            {
                "status": str(status or "execution_unknown"),
                "detail": str(detail or ""),
                "reconciliation_required": bool(reconciliation_required),
                "receipt_file": str(Path(receipt_file).expanduser().resolve()),
                "updated_at": timestamp,
            }
        )
        ledger["updated_at"] = timestamp
        return dict(record)

    return _with_locked_ledger(ledger_path, update)  # type: ignore[return-value]


def _materialize_approved_source_snapshot(proposal: Mapping[str, object]) -> Path:
    _approved_row, execution_source = _validate_proposal_snapshot(proposal)
    proposal_digest = _proposal_digest(proposal)
    snapshot_path = (
        canonical_review_state_root() / "approved-source-snapshots" / f"{proposal_digest}.json"
    )
    if snapshot_path.exists():
        try:
            existing = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Canonical reviewed LinkedIn source snapshot is unreadable") from exc
        if existing != execution_source:
            raise ValueError("Canonical reviewed LinkedIn source snapshot digest collision")
        return snapshot_path
    write_immutable_json(snapshot_path, execution_source)
    return snapshot_path


def _execute_approved_row(approval: Mapping[str, object]) -> dict[str, object]:
    """Enter existing production code with exactly the one immutable approved row."""

    from outreach.cli import (
        _apply_linkedin_cadence_guards,
        execute_invite_batch,
        execute_linkedin_followup_send,
    )
    from outreach.config import OutreachSettings
    from outreach.tracking import OutreachWorkbook

    proposal = approval["proposal"]
    assert isinstance(proposal, dict)
    row, _execution_source = _validate_proposal_snapshot(proposal)
    source_path = _materialize_approved_source_snapshot(proposal)
    action = str(proposal["action"])
    settings = OutreachSettings()

    if action == "invite":
        raw_score = row.get("score", 0)
        try:
            score = int(raw_score or 0)
        except (TypeError, ValueError):
            score = 0
        note_qc = row.get("note_qc") if isinstance(row.get("note_qc"), dict) else {}
        send_artifact, progress_artifact, status_counts, contacts_added, touchpoints_added = (
            execute_invite_batch(
                settings=settings,
                company=str(proposal["company"]),
                source_artifact_path=source_path,
                batch=[row],
                execute=True,
                limit=1,
                start_at=0,
                verdict=str(note_qc.get("verdict") or "reviewed"),
                min_score=score,
                source_payload_snapshot=_execution_source,
            )
        )
        return {
            "action": action,
            "processed_count": 1,
            "send_artifact": str(send_artifact),
            "progress_artifact": str(progress_artifact),
            "status_counts": status_counts,
            "contacts_added": contacts_added,
            "touchpoints_added": touchpoints_added,
        }

    # Re-run the same tracker-backed cadence, duplicate, stop, and learned-
    # negative guard used by the public CLI at the last possible point before
    # entering the live follow-up implementation.
    cadence_allowed, cadence_held = _apply_linkedin_cadence_guards(
        workbook=OutreachWorkbook(settings.resolved_tracking_workspace_dir),
        drafts=[row],
    )
    if len(cadence_allowed) != 1 or cadence_held:
        reasons = []
        for held in cadence_held:
            reasons.extend(str(reason) for reason in held.get("cadence_reasons") or [])
        return {
            "action": action,
            "processed_count": 0,
            "status_counts": {"cadence_blocked": 1},
            "cadence_reasons": reasons or ["Public-equivalent LinkedIn cadence guard blocked."],
        }
    guarded_row = dict(cadence_allowed[0])
    guarded_row["_reviewed_require_exact_thread_id"] = True
    send_artifact, progress_artifact, status_counts, touchpoints_added = (
        execute_linkedin_followup_send(
            settings=settings,
            draft_artifact=source_path,
            drafts=[guarded_row],
            execute=True,
            limit=1,
            start_at=0,
            include_optional=True,
        )
    )
    return {
        "action": action,
        "processed_count": 1,
        "send_artifact": str(send_artifact),
        "progress_artifact": str(progress_artifact),
        "status_counts": status_counts,
        "touchpoints_added": touchpoints_added,
    }


def classify_execution_result(execution: Mapping[str, object]) -> tuple[str, bool, str]:
    raw_counts = execution.get("status_counts")
    counts: dict[str, int] = {}
    if isinstance(raw_counts, dict):
        for raw_status, raw_count in raw_counts.items():
            try:
                count = int(raw_count)
            except (TypeError, ValueError):
                continue
            if count > 0:
                counts[str(raw_status).strip().casefold()] = count
    try:
        processed_count = int(execution.get("processed_count") or 0)
    except (TypeError, ValueError):
        processed_count = 0

    if processed_count == 1 and counts == {"sent": 1}:
        return (
            "execution_completed",
            False,
            "Exactly one reviewed LinkedIn action was confirmed sent.",
        )

    blocked_statuses = {
        "already_connected",
        "cadence_blocked",
        "navigation_error",
        "send_already_reserved",
        "skipped",
        "skipped_latest_changed",
        "unavailable",
    }
    if counts and set(counts).issubset(blocked_statuses):
        return (
            "execution_blocked",
            True,
            "The reviewed LinkedIn action was not confirmed sent and requires reconciliation.",
        )
    return (
        "execution_unknown",
        True,
        "The reviewed LinkedIn action was not exactly one confirmed send and requires reconciliation.",
    )


def execute_approval(
    *,
    approval_file: Path,
    expected_approval_sha256: str,
    receipt_file: Path,
    executor: Callable[[Mapping[str, object]], dict[str, object]] | None = None,
) -> dict[str, object]:
    receipt_path = Path(receipt_file).expanduser()
    if receipt_path.exists():
        raise FileExistsError(f"Reviewed LinkedIn receipt already exists: {receipt_path}")
    approval = load_and_validate_approval(
        approval_file,
        expected_approval_sha256=expected_approval_sha256,
    )
    ledger_path = canonical_execution_ledger_path()
    reservation = reserve_approval_execution(
        ledger_path,
        approval=approval,
        approval_path=approval_file,
    )
    approval_digest = str(approval["approval_sha256"])
    runner = executor or _execute_approved_row
    try:
        execution = runner(approval)
    except Exception as exc:
        detail = (
            "Reviewed LinkedIn execution raised after the approval was consumed; delivery may be "
            f"unknown until reconciled ({type(exc).__name__}: {exc})."
        )
        receipt: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "approval_sha256": approval_digest,
            "proposal_sha256": str(approval["proposal_sha256"]),
            "status": "execution_unknown",
            "reconciliation_required": True,
            "detail": detail,
            "reservation": reservation,
        }
        finalize_approval_execution(
            ledger_path,
            approval_sha256=approval_digest,
            status="execution_unknown",
            detail=detail,
            reconciliation_required=True,
            receipt_file=receipt_path,
        )
        write_immutable_json(receipt_path, receipt)
        raise

    execution_status, reconciliation_required, detail = classify_execution_result(execution)
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "approval_sha256": approval_digest,
        "proposal_sha256": str(approval["proposal_sha256"]),
        "status": execution_status,
        "reconciliation_required": reconciliation_required,
        "detail": detail,
        "reservation": reservation,
        "execution": execution,
    }
    finalize_approval_execution(
        ledger_path,
        approval_sha256=approval_digest,
        status=execution_status,
        detail=str(receipt["detail"]),
        reconciliation_required=reconciliation_required,
        receipt_file=receipt_path,
    )
    write_immutable_json(receipt_path, receipt)
    return receipt


def _outgoing_message(path: Path | None) -> str | None:
    if path is None:
        return None
    return Path(path).expanduser().read_text(encoding="utf-8")


def _proposal_from_args(args: argparse.Namespace) -> dict[str, object]:
    return build_review_proposal(
        action=args.action,
        source_artifact=args.source_artifact,
        row_index=args.row_index,
        outgoing_message=_outgoing_message(args.outgoing_message_file),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview, approve, and execute exactly one review-bound LinkedIn action."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_proposal_args(command: argparse.ArgumentParser) -> None:
        command.add_argument("--action", choices=sorted(SUPPORTED_ACTIONS), required=True)
        command.add_argument("--source-artifact", type=Path, required=True)
        command.add_argument("--row-index", type=int, required=True)
        command.add_argument("--outgoing-message-file", type=Path)

    preview = subparsers.add_parser("preview", help="Render the exact immutable review proposal.")
    add_proposal_args(preview)
    preview.add_argument("--output", type=Path)

    approve = subparsers.add_parser("approve", help="Write one immutable human approval file.")
    add_proposal_args(approve)
    approve.add_argument("--expect-proposal-sha256", required=True)
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--approval-file", type=Path, required=True)

    execute = subparsers.add_parser("execute", help="Consume and execute one approved row.")
    execute.add_argument("--approval-file", type=Path, required=True)
    execute.add_argument("--expect-approval-sha256", required=True)
    execute.add_argument("--receipt-file", type=Path, required=True)
    execute.add_argument(
        "--execute",
        action="store_true",
        help="Required explicit live-execution acknowledgement.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "preview":
        payload = _proposal_from_args(args)
        if args.output:
            atomic_write_json(args.output, payload)
        public_output = {
            "status": "review_required",
            "proposal_sha256": payload["proposal_sha256"],
        }
    elif args.command == "approve":
        proposal = _proposal_from_args(args)
        payload = write_approval(
            args.approval_file,
            proposal=proposal,
            expected_proposal_sha256=args.expect_proposal_sha256,
            approved_by=args.approved_by,
        )
        public_output = {
            "status": "approved",
            "proposal_sha256": payload["proposal_sha256"],
            "approval_sha256": payload["approval_sha256"],
        }
    else:
        if not args.execute:
            raise ValueError("Reviewed LinkedIn live execution requires --execute")
        payload = execute_approval(
            approval_file=args.approval_file,
            expected_approval_sha256=args.expect_approval_sha256,
            receipt_file=args.receipt_file,
        )
        public_output = {
            "status": payload["status"],
            "proposal_sha256": payload["proposal_sha256"],
            "approval_sha256": payload["approval_sha256"],
            "reconciliation_required": payload["reconciliation_required"],
        }
    print(json.dumps(public_output, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, Mapping
from urllib.parse import urlsplit, urlunsplit


SCHEMA_VERSION = 1
DEFAULT_FILENAME = "linkedin_invite_send_reservations.json"
UNRESOLVED_STATUSES = {"attempt_reserved", "send_unknown_reserved"}
KNOWN_SEND_STATUSES = {
    "sent",
    "sent_without_note",
    "already_connected",
    "reconciled_connected",
    "reconciled_pending",
}


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def reservation_ledger_path(workspace: Path) -> Path:
    return Path(workspace) / DEFAULT_FILENAME


def reservation_key(*, linkedin_url: str, company: str, name: str) -> str:
    profile = _canonical_linkedin_profile(linkedin_url)
    identity = profile or "|".join(
        (
            _identity_text(company),
            _identity_text(name),
        )
    )
    if not identity.strip("|"):
        raise ValueError("Invite reservation requires a LinkedIn URL or person/company identity")
    return "invite-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def reserve_invite_attempt(
    path: Path,
    *,
    company: str,
    candidate: Mapping[str, object],
    source_artifact: str,
    progress_artifact: str,
    now: str | None = None,
) -> tuple[dict[str, object], bool]:
    """Atomically reserve one candidate before any child process can click Send."""

    timestamp = now or utc_now_iso()
    linkedin_url = str(candidate.get("linkedin_url") or "").strip()
    name = str(candidate.get("name") or "Unknown").strip()
    key = reservation_key(linkedin_url=linkedin_url, company=company, name=name)
    with _locked_payload(path) as payload:
        reservations = payload["reservations"]
        existing = reservations.get(key)
        if isinstance(existing, dict) and _auto_retry_blocked(existing):
            if str(existing.get("status") or "") == "attempt_reserved":
                existing.update(
                    {
                        "status": "send_unknown_reserved",
                        "reconciliation_required": True,
                        "detail": (
                            "A prior parent process ended after reserving this invite; "
                            "delivery is unknown until signed-in reconciliation."
                        ),
                        "updated_at": timestamp,
                    }
                )
                payload["updated_at"] = timestamp
            return dict(existing), False

        attempt_id = hashlib.sha256(
            f"{key}|{timestamp}|{source_artifact}|{progress_artifact}".encode("utf-8")
        ).hexdigest()[:24]
        reservation: dict[str, object] = {
            "reservation_key": key,
            "attempt_id": attempt_id,
            "company": company,
            "name": name,
            "linkedin_url": linkedin_url,
            "source_artifact": source_artifact,
            "progress_artifact": progress_artifact,
            "status": "attempt_reserved",
            "reconciliation_required": True,
            "detail": "Slot reserved before launching the killable invite worker.",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        reservations[key] = reservation
        payload["updated_at"] = timestamp
        return dict(reservation), True


def finalize_invite_attempt(
    path: Path,
    *,
    reservation_key_value: str,
    attempt_id: str,
    status: str,
    detail: str,
    now: str | None = None,
) -> dict[str, object]:
    timestamp = now or utc_now_iso()
    with _locked_payload(path) as payload:
        reservation = payload["reservations"].get(reservation_key_value)
        if not isinstance(reservation, dict):
            raise ValueError(f"Invite reservation disappeared: {reservation_key_value}")
        if str(reservation.get("attempt_id") or "") != attempt_id:
            raise ValueError(
                f"Invite reservation attempt changed concurrently: {reservation_key_value}"
            )
        normalized = str(status or "send_unknown_reserved").strip().casefold()
        reconciliation_required = normalized in UNRESOLVED_STATUSES
        reservation.update(
            {
                "status": normalized,
                "detail": detail,
                "reconciliation_required": reconciliation_required,
                "updated_at": timestamp,
            }
        )
        payload["updated_at"] = timestamp
        return dict(reservation)


def reconcile_invite_reservation(
    path: Path,
    *,
    linkedin_url: str,
    status: str,
    detail: str = "",
    now: str | None = None,
) -> dict[str, object] | None:
    """Resolve an uncertain reservation only from an explicit signed-in result."""

    profile = _canonical_linkedin_profile(linkedin_url)
    if not profile:
        return None
    timestamp = now or utc_now_iso()
    normalized = str(status or "").strip().casefold()
    status_map = {
        "connected": "reconciled_connected",
        "replied": "reconciled_connected",
        "pending": "reconciled_pending",
        "not_connected": "reconciled_not_connected",
    }
    resolved_status = status_map.get(normalized)
    if resolved_status is None:
        return None
    with _locked_payload(path) as payload:
        for reservation in payload["reservations"].values():
            if not isinstance(reservation, dict):
                continue
            if _canonical_linkedin_profile(str(reservation.get("linkedin_url") or "")) != profile:
                continue
            reservation.update(
                {
                    "status": resolved_status,
                    "reconciliation_required": False,
                    "reconciled_status": normalized,
                    "reconciled_detail": detail,
                    "reconciled_at": timestamp,
                    "updated_at": timestamp,
                }
            )
            payload["updated_at"] = timestamp
            return dict(reservation)
    return None


def load_invite_reservations(path: Path) -> dict[str, object]:
    """Read the ledger for reporting/tests; malformed state fails closed."""

    return _load_payload(Path(path))


def atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temp_path = Path(raw_path)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _auto_retry_blocked(reservation: Mapping[str, object]) -> bool:
    status = str(reservation.get("status") or "").strip().casefold()
    return bool(reservation.get("reconciliation_required")) or status in (
        UNRESOLVED_STATUSES | KNOWN_SEND_STATUSES
    )


@contextmanager
def _locked_payload(path: Path) -> Iterator[dict[str, object]]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        payload = _load_payload(path)
        before = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        try:
            yield payload
        finally:
            after = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            if after != before:
                atomic_write_json(path, payload)
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _load_payload(path: Path) -> dict[str, object]:
    if not path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": "",
            "reservations": {},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invite reservation ledger is unreadable: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Invite reservation ledger has an unsupported schema: {path}")
    reservations = payload.get("reservations")
    if not isinstance(reservations, dict):
        raise ValueError(f"Invite reservation ledger has invalid reservations: {path}")
    for key, reservation in reservations.items():
        if not isinstance(key, str) or not isinstance(reservation, dict):
            raise ValueError(f"Invite reservation ledger contains an invalid row: {path}")
        if str(reservation.get("reservation_key") or "") != key:
            raise ValueError(f"Invite reservation ledger key mismatch for {key}: {path}")
    return payload


def _canonical_linkedin_profile(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    host = (parts.hostname or "").casefold()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return ""
    path = parts.path.rstrip("/").casefold()
    if not path.startswith(("/in/", "/pub/")):
        return ""
    return urlunsplit(("https", "www.linkedin.com", path, "", ""))


def _identity_text(value: str) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())

from __future__ import annotations

import argparse
import json
from pathlib import Path

from outreach.config import OutreachSettings
from outreach.invite_reservations import atomic_write_json
from outreach.services.linkedin import LinkedInScraper


WORKER_SCHEMA_VERSION = 1


def _preflight_failure_result(candidate: dict, detail: str) -> dict[str, object]:
    """Retryable failure before LinkedIn could have clicked Send."""

    return {
        "name": str(candidate.get("name") or "Unknown"),
        "linkedin_url": str(candidate.get("linkedin_url") or ""),
        "status": "preflight_failed",
        "detail": detail,
        "note": str(candidate.get("note") or ""),
        "screenshot_path": None,
        "reservation_reused": False,
    }


def run_worker(input_path: Path, output_path: Path) -> dict[str, object]:
    """Execute exactly one live candidate inside a killable process boundary."""

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != WORKER_SCHEMA_VERSION:
        raise ValueError("Invite worker input has an unsupported schema")
    candidate = payload.get("candidate")
    if not isinstance(candidate, dict):
        raise ValueError("Invite worker input requires one candidate object")
    if payload.get("execute") is not True:
        raise ValueError("Invite worker only accepts execute=true live attempts")

    settings = OutreachSettings()
    try:
        results = LinkedInScraper(settings).send_connection_requests(
            [candidate],
            execute=True,
        )
    except Exception as exc:
        message = str(exc)
        # chrome-error / CDP / auth preflight never reaches Send — do not crash
        # the worker into send_unknown_reserved (which freezes the slot).
        if any(
            marker in message.casefold()
            for marker in (
                "preflight failed",
                "chrome-error://",
                "could not attach to chrome",
                "nothing is listening",
                "authwall",
                "login page",
            )
        ):
            output = {
                "schema_version": WORKER_SCHEMA_VERSION,
                "result": _preflight_failure_result(
                    candidate,
                    f"Invite preflight failed before send (retryable): {exc}",
                ),
            }
            atomic_write_json(output_path, output)
            return output
        raise
    if len(results) != 1:
        raise RuntimeError(f"Invite worker expected one result, got {len(results)}")
    output = {
        "schema_version": WORKER_SCHEMA_VERSION,
        "result": results[0].__dict__,
    }
    atomic_write_json(output_path, output)
    return output


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one live LinkedIn invite candidate.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_worker(args.input, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

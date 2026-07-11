from __future__ import annotations

import argparse
import json
from pathlib import Path

from outreach.config import OutreachSettings
from outreach.invite_reservations import atomic_write_json
from outreach.services.linkedin import LinkedInScraper


WORKER_SCHEMA_VERSION = 1


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
    results = LinkedInScraper(settings).send_connection_requests(
        [candidate],
        execute=True,
    )
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

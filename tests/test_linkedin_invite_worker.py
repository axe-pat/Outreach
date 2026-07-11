from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from outreach.linkedin_invite_worker import run_worker
from outreach.services.linkedin import InviteSendResult


def test_worker_contract_executes_exactly_one_candidate(monkeypatch, tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    candidate = {
        "name": "Worker Person",
        "linkedin_url": "https://www.linkedin.com/in/worker-person/",
        "note": "Hello",
    }
    input_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "execute": True,
                "candidate": candidate,
            }
        ),
        encoding="utf-8",
    )
    calls: list[tuple[list[dict], bool]] = []

    def fake_send(_self, candidates, execute=False, on_result=None):
        calls.append((candidates, execute))
        assert on_result is None
        return [
            InviteSendResult(
                name="Worker Person",
                linkedin_url=candidate["linkedin_url"],
                status="sent",
                detail="sent",
                note="Hello",
            )
        ]

    monkeypatch.setattr(
        "outreach.linkedin_invite_worker.LinkedInScraper.send_connection_requests",
        fake_send,
    )

    output = run_worker(input_path, output_path)

    assert calls == [([candidate], True)]
    assert output["result"]["status"] == "sent"
    assert json.loads(output_path.read_text(encoding="utf-8")) == output


def test_worker_contract_rejects_dry_run_input(tmp_path: Path) -> None:
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "execute": False,
                "candidate": {"name": "No Send"},
            }
        ),
        encoding="utf-8",
    )

    try:
        run_worker(input_path, tmp_path / "output.json")
    except ValueError as exc:
        assert "execute=true" in str(exc)
    else:
        raise AssertionError("worker accepted a dry-run input")


def test_worker_module_argv_handles_paths_with_spaces_without_live_send(
    tmp_path: Path,
) -> None:
    spaced_dir = tmp_path / "worker path with spaces"
    spaced_dir.mkdir()
    input_path = spaced_dir / "input payload.json"
    output_path = spaced_dir / "output payload.json"
    input_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "execute": False,
                "candidate": {"name": "No Send"},
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "outreach.linkedin_invite_worker",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "execute=true" in result.stderr
    assert not output_path.exists()

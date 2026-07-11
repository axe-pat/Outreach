from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from outreach.reviewed_linkedin import (
    ReplayProtectedError,
    _execute_approved_row,
    build_review_proposal,
    canonical_execution_ledger_path,
    classify_execution_result,
    create_approval,
    execute_approval,
    file_sha256,
    load_and_validate_approval,
    main,
    write_approval,
)
from outreach.config import OutreachSettings
from outreach.services.linkedin import LinkedInMessageThread, LinkedInScraper
from outreach.tracking import (
    ContactRecord,
    OutreachChannel,
    OutreachWorkbook,
    TouchpointRecord,
)


@pytest.fixture(autouse=True)
def _isolated_configured_workspace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRACKING_WORKSPACE_DIR", str(tmp_path / "workspace"))


def _invite_source(tmp_path: Path) -> Path:
    source = tmp_path / "invite notes.json"
    source.write_text(
        json.dumps(
            {
                "company": "Acme",
                "company_mode": "default",
                "results": [
                    {
                        "name": "Maya Singh",
                        "linkedin_url": "https://linkedin.com/in/Maya-Singh/?trk=test",
                        "score": 72,
                        "note": "Hi Maya, your Acme product path stood out. Open to connecting?",
                        "note_qc": {"verdict": "send"},
                    },
                    {
                        "name": "Do Not Send",
                        "linkedin_url": "https://linkedin.com/in/not-reviewed/",
                        "note": "This row was not reviewed.",
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return source


def _followup_source(tmp_path: Path) -> Path:
    source = tmp_path / "followups.json"
    source.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "contact_id": "ct-maya",
                        "organization_id": "org-acme",
                        "company": "Acme",
                        "name": "Maya Singh",
                        "linkedin_url": "https://www.linkedin.com/in/maya-singh/",
                        "thread_id": "2-abc",
                        "thread_url": "https://linkedin.com/messaging/thread/2-abc/?trk=inbox",
                        "latest_message": "Happy to help. What are you targeting?",
                        "last_sender": "Maya",
                        "timestamp_text": "9:14 AM",
                        "message_window": [
                            {
                                "sender": "You",
                                "message": "Thanks for connecting.",
                                "source": "original_invite",
                            },
                            {
                                "sender": "Maya",
                                "message": "Happy to help. What are you targeting?",
                                "timestamp_text": "9:14 AM",
                                "source": "linkedin_latest",
                            },
                        ],
                        "draft_message": (
                            "Thanks Maya. I'm targeting technical product roles where my data "
                            "platform background is useful."
                        ),
                        "send_recommendation": "send",
                        "source_status": "accepted",
                    }
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return source


def test_invite_proposal_binds_exact_artifact_recipient_company_and_message(
    tmp_path: Path,
) -> None:
    source = _invite_source(tmp_path)

    proposal = build_review_proposal(
        action="invite",
        source_artifact=source,
        row_index=0,
    )

    assert proposal["source_artifact"] == str(source.resolve())
    assert proposal["source_sha256"] == file_sha256(source)
    assert proposal["source_row_index"] == 0
    assert proposal["company"] == "Acme"
    assert proposal["recipient"] == {
        "name": "Maya Singh",
        "linkedin_profile": "https://www.linkedin.com/in/Maya-Singh",
        "contact_id": "",
        "organization_id": "",
        "thread_id": "",
        "thread_url": "",
    }
    assert proposal["latest_inbound_context"] is None
    assert proposal["outgoing_message"] == (
        "Hi Maya, your Acme product path stood out. Open to connecting?"
    )
    assert proposal["workspace_root"] == str((tmp_path / "workspace").resolve())
    assert proposal["approved_row_snapshot"]["name"] == "Maya Singh"
    assert proposal["execution_source_snapshot"]["results"] == [proposal["approved_row_snapshot"]]
    assert len(str(proposal["proposal_sha256"])) == 64


def test_followup_proposal_binds_thread_contact_and_latest_inbound_context(
    tmp_path: Path,
) -> None:
    source = _followup_source(tmp_path)

    proposal = build_review_proposal(
        action="followup",
        source_artifact=source,
        row_index=0,
    )

    recipient = proposal["recipient"]
    assert isinstance(recipient, dict)
    assert recipient["contact_id"] == "ct-maya"
    assert recipient["thread_id"] == "2-abc"
    assert recipient["thread_url"] == "https://www.linkedin.com/messaging/thread/2-abc"
    context = proposal["latest_inbound_context"]
    assert isinstance(context, dict)
    assert context["observed_latest_message"] == "Happy to help. What are you targeting?"
    assert context["latest_inbound"] == {
        "sender": "Maya",
        "message": "Happy to help. What are you targeting?",
        "timestamp_text": "9:14 AM",
        "source": "linkedin_latest",
    }


@pytest.mark.parametrize("thread_id", ["", "synthetic:maya-singh"])
def test_followup_proposal_rejects_missing_or_synthetic_thread_id(
    tmp_path: Path,
    thread_id: str,
) -> None:
    source = _followup_source(tmp_path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["results"][0]["thread_id"] = thread_id
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="exact non-synthetic thread_id"):
        build_review_proposal(
            action="followup",
            source_artifact=source,
            row_index=0,
        )


def test_reviewed_thread_lookup_never_falls_back_to_matching_name(monkeypatch) -> None:
    scraper = LinkedInScraper(OutreachSettings())

    class FakePage:
        def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

    wrong_thread = LinkedInMessageThread(
        thread_id="different-thread",
        name="Maya Singh",
        thread_url="https://www.linkedin.com/messaging/thread/different-thread/",
        latest_message="Happy to help. What are you targeting?",
        last_sender="Maya",
        timestamp_text="9:14 AM",
        unread=True,
    )
    monkeypatch.setattr(
        scraper, "_scroll_message_list", lambda *_args, **_kwargs: {"at_bottom": True}
    )
    monkeypatch.setattr(
        scraper, "_extract_message_threads", lambda *_args, **_kwargs: [wrong_thread]
    )

    reviewed = scraper._find_message_thread_for_draft(
        FakePage(),
        draft={
            "thread_id": "approved-thread",
            "name": "Maya Singh",
            "_reviewed_require_exact_thread_id": True,
        },
        max_scrolls=0,
    )
    legacy = scraper._find_message_thread_for_draft(
        FakePage(),
        draft={"thread_id": "approved-thread", "name": "Maya Singh"},
        max_scrolls=0,
    )

    assert reviewed is None
    assert legacy == wrong_thread


def test_reviewed_thread_click_disables_name_fallback() -> None:
    scraper = LinkedInScraper(OutreachSettings())
    captured: dict[str, object] = {}

    class FakePage:
        def evaluate(self, script: str, payload: dict[str, object]) -> dict[str, object]:
            captured["script"] = script
            captured["payload"] = payload
            return {"ok": False}

    thread = LinkedInMessageThread(
        thread_id="approved-thread",
        name="Maya Singh",
        thread_url="https://www.linkedin.com/messaging/thread/approved-thread/",
        latest_message="Latest",
    )

    assert scraper._click_message_thread(FakePage(), thread, exact_thread_only=True) is False
    assert captured["payload"] == {
        "threadId": "approved-thread",
        "name": "Maya Singh",
        "exactThreadOnly": True,
    }
    assert "!exactThreadOnly" in str(captured["script"])


def test_reviewed_empty_latest_blocks_new_live_inbound(monkeypatch) -> None:
    scraper = LinkedInScraper(OutreachSettings())

    class FakePage:
        url = "https://www.linkedin.com/messaging/"

    live_thread = LinkedInMessageThread(
        thread_id="approved-thread",
        name="Maya Singh",
        thread_url="https://www.linkedin.com/messaging/thread/approved-thread/",
        latest_message="NEW INBOUND AFTER APPROVAL",
        last_sender="Maya",
    )
    monkeypatch.setattr(
        scraper,
        "_find_message_thread_for_draft",
        lambda *_args, **_kwargs: live_thread,
    )

    result = scraper._send_single_followup_from_messages(
        FakePage(),
        draft={
            "thread_id": "approved-thread",
            "name": "Maya Singh",
            "latest_message": "",
            "draft_message": "Approved reply",
            "_reviewed_require_exact_thread_id": True,
        },
        execute=False,
        max_scrolls=0,
    )

    assert result.status == "skipped_latest_changed"
    assert result.live_latest_message == "NEW INBOUND AFTER APPROVAL"


def test_approval_requires_preview_digest_and_is_immutable(tmp_path: Path) -> None:
    source = _invite_source(tmp_path)
    proposal = build_review_proposal(
        action="invite",
        source_artifact=source,
        row_index=0,
    )
    approval_path = tmp_path / "approval.json"

    with pytest.raises(ValueError, match="changed after human review"):
        create_approval(
            proposal=proposal,
            expected_proposal_sha256="0" * 64,
            approved_by="operator",
        )

    approval = write_approval(
        approval_path,
        proposal=proposal,
        expected_proposal_sha256=str(proposal["proposal_sha256"]),
        approved_by="operator",
        approved_at="2026-07-11T10:00:00+00:00",
    )
    assert approval_path.stat().st_mode & 0o777 == 0o600
    loaded = load_and_validate_approval(
        approval_path,
        expected_approval_sha256=str(approval["approval_sha256"]),
    )
    assert loaded == approval
    with pytest.raises(FileExistsError):
        write_approval(
            approval_path,
            proposal=proposal,
            expected_proposal_sha256=str(proposal["proposal_sha256"]),
            approved_by="operator",
        )


def test_approval_cannot_move_to_a_fresh_workspace_ledger(tmp_path: Path, monkeypatch) -> None:
    source = _invite_source(tmp_path)
    proposal = build_review_proposal(
        action="invite",
        source_artifact=source,
        row_index=0,
    )
    approval_path = tmp_path / "approval.json"
    approval = write_approval(
        approval_path,
        proposal=proposal,
        expected_proposal_sha256=str(proposal["proposal_sha256"]),
        approved_by="operator",
    )
    monkeypatch.setenv("TRACKING_WORKSPACE_DIR", str(tmp_path / "different-workspace"))

    with pytest.raises(ValueError, match="different configured workspace"):
        execute_approval(
            approval_file=approval_path,
            expected_approval_sha256=str(approval["approval_sha256"]),
            receipt_file=tmp_path / "receipt.json",
            executor=lambda _approval: pytest.fail("moved approval reached executor"),
        )


def test_source_mutation_cannot_change_immutable_approved_execution_row(tmp_path: Path) -> None:
    source = _invite_source(tmp_path)
    proposal = build_review_proposal(
        action="invite",
        source_artifact=source,
        row_index=0,
    )
    approval_path = tmp_path / "approval.json"
    approval = write_approval(
        approval_path,
        proposal=proposal,
        expected_proposal_sha256=str(proposal["proposal_sha256"]),
        approved_by="operator",
    )
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["results"][0]["note"] = "Mutated after review"
    source.write_text(json.dumps(payload), encoding="utf-8")
    seen: list[str] = []

    def inspect_snapshot(received: object) -> dict[str, object]:
        assert isinstance(received, dict)
        proposal_snapshot = received["proposal"]
        assert isinstance(proposal_snapshot, dict)
        row = proposal_snapshot["approved_row_snapshot"]
        assert isinstance(row, dict)
        seen.append(str(row["note"]))
        return {"processed_count": 1, "status_counts": {"sent": 1}}

    receipt = execute_approval(
        approval_file=approval_path,
        expected_approval_sha256=str(approval["approval_sha256"]),
        receipt_file=tmp_path / "receipt.json",
        executor=inspect_snapshot,
    )
    assert receipt["status"] == "execution_completed"
    assert seen == ["Hi Maya, your Acme product path stood out. Open to connecting?"]


def test_execution_consumes_before_executor_and_blocks_replay(tmp_path: Path) -> None:
    source = _followup_source(tmp_path)
    proposal = build_review_proposal(
        action="followup",
        source_artifact=source,
        row_index=0,
    )
    approval_path = tmp_path / "approval.json"
    approval = write_approval(
        approval_path,
        proposal=proposal,
        expected_proposal_sha256=str(proposal["proposal_sha256"]),
        approved_by="operator",
    )
    ledger_path = canonical_execution_ledger_path()
    calls: list[str] = []

    def fake_executor(received: object) -> dict[str, object]:
        assert received == approval
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        record = ledger["executions"][approval["approval_sha256"]]
        assert record["status"] == "execution_reserved"
        calls.append("executed")
        return {"processed_count": 1, "status_counts": {"sent": 1}}

    receipt = execute_approval(
        approval_file=approval_path,
        expected_approval_sha256=str(approval["approval_sha256"]),
        receipt_file=tmp_path / "receipt.json",
        executor=fake_executor,
    )

    assert receipt["status"] == "execution_completed"
    assert receipt["execution"]["processed_count"] == 1
    assert calls == ["executed"]
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["executions"][approval["approval_sha256"]]["status"] == (
        "execution_completed"
    )

    with pytest.raises(ReplayProtectedError, match="already consumed"):
        execute_approval(
            approval_file=approval_path,
            expected_approval_sha256=str(approval["approval_sha256"]),
            receipt_file=tmp_path / "second-receipt.json",
            executor=fake_executor,
        )
    assert calls == ["executed"]


def test_second_approval_for_same_proposal_cannot_execute(tmp_path: Path) -> None:
    source = _invite_source(tmp_path)
    proposal = build_review_proposal(
        action="invite",
        source_artifact=source,
        row_index=0,
    )
    first_path = tmp_path / "first-approval.json"
    second_path = tmp_path / "second-approval.json"
    first = write_approval(
        first_path,
        proposal=proposal,
        expected_proposal_sha256=str(proposal["proposal_sha256"]),
        approved_by="operator-one",
        approved_at="2026-07-11T10:00:00+00:00",
    )
    second = write_approval(
        second_path,
        proposal=proposal,
        expected_proposal_sha256=str(proposal["proposal_sha256"]),
        approved_by="operator-two",
        approved_at="2026-07-11T10:05:00+00:00",
    )
    calls: list[str] = []

    def sent(_approval: object) -> dict[str, object]:
        calls.append("sent")
        return {"processed_count": 1, "status_counts": {"sent": 1}}

    execute_approval(
        approval_file=first_path,
        expected_approval_sha256=str(first["approval_sha256"]),
        receipt_file=tmp_path / "first-receipt.json",
        executor=sent,
    )

    with pytest.raises(ReplayProtectedError, match="proposal was already consumed"):
        execute_approval(
            approval_file=second_path,
            expected_approval_sha256=str(second["approval_sha256"]),
            receipt_file=tmp_path / "second-receipt.json",
            executor=sent,
        )
    assert calls == ["sent"]


def test_non_sent_execution_is_blocked_and_marked_for_reconciliation(tmp_path: Path) -> None:
    source = _invite_source(tmp_path)
    proposal = build_review_proposal(
        action="invite",
        source_artifact=source,
        row_index=0,
    )
    approval_path = tmp_path / "approval.json"
    approval = write_approval(
        approval_path,
        proposal=proposal,
        expected_proposal_sha256=str(proposal["proposal_sha256"]),
        approved_by="operator",
    )

    receipt = execute_approval(
        approval_file=approval_path,
        expected_approval_sha256=str(approval["approval_sha256"]),
        receipt_file=tmp_path / "receipt.json",
        executor=lambda _approval: {
            "processed_count": 1,
            "status_counts": {"skipped_latest_changed": 1},
        },
    )

    assert receipt["status"] == "execution_blocked"
    assert receipt["reconciliation_required"] is True
    ledger = json.loads(canonical_execution_ledger_path().read_text(encoding="utf-8"))
    record = ledger["executions"][approval["approval_sha256"]]
    assert record["status"] == "execution_blocked"
    assert record["reconciliation_required"] is True


def test_executor_failure_is_unknown_and_still_replay_protected(tmp_path: Path) -> None:
    source = _invite_source(tmp_path)
    proposal = build_review_proposal(
        action="invite",
        source_artifact=source,
        row_index=0,
    )
    approval_path = tmp_path / "approval.json"
    approval = write_approval(
        approval_path,
        proposal=proposal,
        expected_proposal_sha256=str(proposal["proposal_sha256"]),
        approved_by="operator",
    )
    ledger = canonical_execution_ledger_path()
    receipt = tmp_path / "receipt.json"

    def crash(_approval: object) -> dict[str, object]:
        raise RuntimeError("worker disappeared")

    with pytest.raises(RuntimeError, match="worker disappeared"):
        execute_approval(
            approval_file=approval_path,
            expected_approval_sha256=str(approval["approval_sha256"]),
            receipt_file=receipt,
            executor=crash,
        )
    assert json.loads(receipt.read_text(encoding="utf-8"))["status"] == "execution_unknown"
    record = json.loads(ledger.read_text(encoding="utf-8"))["executions"]
    assert record[approval["approval_sha256"]]["status"] == "execution_unknown"
    with pytest.raises(ReplayProtectedError):
        execute_approval(
            approval_file=approval_path,
            expected_approval_sha256=str(approval["approval_sha256"]),
            receipt_file=tmp_path / "retry.json",
            executor=crash,
        )


@pytest.mark.parametrize(
    ("execution", "expected_status"),
    [
        ({"processed_count": 1, "status_counts": {"sent": 1}}, "execution_completed"),
        ({"processed_count": 1, "status_counts": {"skipped": 1}}, "execution_blocked"),
        ({"processed_count": 0, "status_counts": {"cadence_blocked": 1}}, "execution_blocked"),
        ({"processed_count": 1, "status_counts": {"send_error": 1}}, "execution_unknown"),
        ({"processed_count": 1, "status_counts": {"sent_without_note": 1}}, "execution_unknown"),
        ({"processed_count": 1, "status_counts": {"sent": 1, "skipped": 1}}, "execution_unknown"),
    ],
)
def test_only_exactly_one_literal_sent_is_complete(
    execution: dict[str, object],
    expected_status: str,
) -> None:
    status, reconciliation_required, _detail = classify_execution_result(execution)

    assert status == expected_status
    assert reconciliation_required is (expected_status != "execution_completed")


def test_cli_preview_stdout_is_digest_only_and_has_no_pii(
    tmp_path: Path,
    capsys,
) -> None:
    source = _followup_source(tmp_path)
    proposal_file = tmp_path / "proposal.json"

    assert (
        main(
            [
                "preview",
                "--action",
                "followup",
                "--source-artifact",
                str(source),
                "--row-index",
                "0",
                "--output",
                str(proposal_file),
            ]
        )
        == 0
    )

    stdout = capsys.readouterr().out
    public = json.loads(stdout)
    assert set(public) == {"status", "proposal_sha256"}
    assert public["status"] == "review_required"
    assert "Maya" not in stdout
    assert "Happy to help" not in stdout
    assert json.loads(proposal_file.read_text(encoding="utf-8"))["recipient"]["name"] == (
        "Maya Singh"
    )

    approval_file = tmp_path / "approval.json"
    assert (
        main(
            [
                "approve",
                "--action",
                "followup",
                "--source-artifact",
                str(source),
                "--row-index",
                "0",
                "--expect-proposal-sha256",
                public["proposal_sha256"],
                "--approved-by",
                "operator",
                "--approval-file",
                str(approval_file),
            ]
        )
        == 0
    )
    approval_stdout = capsys.readouterr().out
    approval_public = json.loads(approval_stdout)
    assert set(approval_public) == {"status", "proposal_sha256", "approval_sha256"}
    assert "Maya" not in approval_stdout
    assert "Happy to help" not in approval_stdout


def test_cli_execute_has_no_caller_selectable_ledger() -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "execute",
                "--approval-file",
                "approval.json",
                "--expect-approval-sha256",
                "0" * 64,
                "--ledger",
                "fresh-ledger.json",
                "--receipt-file",
                "receipt.json",
                "--execute",
            ]
        )


def test_production_adapter_passes_exactly_one_invite_row(monkeypatch, tmp_path: Path) -> None:
    source = _invite_source(tmp_path)
    proposal = build_review_proposal(
        action="invite",
        source_artifact=source,
        row_index=0,
        outgoing_message="Exact reviewed edit",
    )
    approval = create_approval(
        proposal=proposal,
        expected_proposal_sha256=str(proposal["proposal_sha256"]),
        approved_by="operator",
    )
    captured: dict[str, object] = {}

    def fake_execute_invite_batch(**kwargs):
        captured.update(kwargs)
        return tmp_path / "send.json", tmp_path / "progress.json", {"sent": 1}, 1, 1

    monkeypatch.setattr("outreach.cli.execute_invite_batch", fake_execute_invite_batch)

    result = _execute_approved_row(approval)

    assert captured["execute"] is True
    assert captured["limit"] == 1
    assert captured["start_at"] == 0
    assert captured["company"] == "Acme"
    assert captured["source_payload_snapshot"] == proposal["execution_source_snapshot"]
    assert len(captured["batch"]) == 1
    assert captured["batch"][0]["name"] == "Maya Singh"
    assert captured["batch"][0]["note"] == "Exact reviewed edit"
    assert result["processed_count"] == 1


def test_production_adapter_passes_exactly_one_followup_row(monkeypatch, tmp_path: Path) -> None:
    source = _followup_source(tmp_path)
    proposal = build_review_proposal(
        action="followup",
        source_artifact=source,
        row_index=0,
    )
    approval = create_approval(
        proposal=proposal,
        expected_proposal_sha256=str(proposal["proposal_sha256"]),
        approved_by="operator",
    )
    captured: dict[str, object] = {}
    guard_calls: list[list[dict[str, object]]] = []

    def fake_guard(*, workbook, drafts):
        assert workbook.base_dir == tmp_path / "workspace"
        guard_calls.append(drafts)
        return drafts, []

    def fake_execute_followup(**kwargs):
        captured.update(kwargs)
        return tmp_path / "send.json", tmp_path / "progress.json", {"sent": 1}, 1

    monkeypatch.setattr(
        "outreach.cli.execute_linkedin_followup_send",
        fake_execute_followup,
    )
    monkeypatch.setattr("outreach.cli._apply_linkedin_cadence_guards", fake_guard)

    result = _execute_approved_row(approval)

    assert captured["execute"] is True
    assert captured["limit"] == 1
    assert captured["start_at"] == 0
    assert captured["include_optional"] is True
    assert len(guard_calls) == 1
    assert len(captured["drafts"]) == 1
    assert captured["drafts"][0]["contact_id"] == "ct-maya"
    assert captured["drafts"][0]["draft_message"] == proposal["outgoing_message"]
    assert captured["drafts"][0]["_reviewed_require_exact_thread_id"] is True
    assert result["processed_count"] == 1


def _tracked_followup_workbook(tmp_path: Path) -> OutreachWorkbook:
    workbook = OutreachWorkbook(tmp_path / "workspace")
    workbook.initialize()
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-maya",
            organization_id="org-acme",
            full_name="Maya Singh",
            linkedin_url="https://www.linkedin.com/in/maya-singh/",
        )
    )
    return workbook


def _append_linkedin_touch(
    workbook: OutreachWorkbook,
    *,
    touchpoint_id: str,
    days_ago: int,
    status: str,
    message_kind: str,
    message_text: str,
) -> None:
    timestamp = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    workbook.append_touchpoint(
        TouchpointRecord(
            touchpoint_id=touchpoint_id,
            organization_id="org-acme",
            contact_id="ct-maya",
            channel=OutreachChannel.LINKEDIN,
            status=status,
            message_kind=message_kind,
            message_text=message_text,
            recorded_at=timestamp,
            sent_at=timestamp,
        )
    )


def test_reviewed_followup_rechecks_terminal_stop_before_live_call(
    monkeypatch, tmp_path: Path
) -> None:
    workbook = _tracked_followup_workbook(tmp_path)
    _append_linkedin_touch(
        workbook,
        touchpoint_id="tp-invite",
        days_ago=12,
        status="Sent",
        message_kind="linkedin_invite",
        message_text="Initial invite",
    )
    _append_linkedin_touch(
        workbook,
        touchpoint_id="tp-accepted",
        days_ago=10,
        status="Connected",
        message_kind="connection_accepted",
        message_text="Invite accepted",
    )
    _append_linkedin_touch(
        workbook,
        touchpoint_id="tp-stop",
        days_ago=1,
        status="Do Not Contact",
        message_kind="do_not_contact",
        message_text="Do not contact",
    )
    source = _followup_source(tmp_path)
    proposal = build_review_proposal(
        action="followup",
        source_artifact=source,
        row_index=0,
    )
    approval = create_approval(
        proposal=proposal,
        expected_proposal_sha256=str(proposal["proposal_sha256"]),
        approved_by="operator",
    )
    monkeypatch.setattr(
        "outreach.cli.execute_linkedin_followup_send",
        lambda **_kwargs: pytest.fail("terminal-stopped follow-up reached live sender"),
    )

    result = _execute_approved_row(approval)

    assert result["processed_count"] == 0
    assert result["status_counts"] == {"cadence_blocked": 1}
    assert any("stop" in reason.casefold() for reason in result["cadence_reasons"])


def test_reviewed_followup_rechecks_duplicate_guard_before_live_call(
    monkeypatch, tmp_path: Path
) -> None:
    workbook = _tracked_followup_workbook(tmp_path)
    _append_linkedin_touch(
        workbook,
        touchpoint_id="tp-invite",
        days_ago=15,
        status="Sent",
        message_kind="linkedin_invite",
        message_text="Initial invite",
    )
    _append_linkedin_touch(
        workbook,
        touchpoint_id="tp-accepted",
        days_ago=12,
        status="Connected",
        message_kind="connection_accepted",
        message_text="Invite accepted",
    )
    duplicate_message = (
        "Thanks Maya. I'm targeting technical product roles where my data platform background "
        "is useful."
    )
    _append_linkedin_touch(
        workbook,
        touchpoint_id="tp-followup",
        days_ago=5,
        status="Sent",
        message_kind="linkedin_followup",
        message_text=duplicate_message,
    )
    source = _followup_source(tmp_path)
    proposal = build_review_proposal(
        action="followup",
        source_artifact=source,
        row_index=0,
    )
    approval = create_approval(
        proposal=proposal,
        expected_proposal_sha256=str(proposal["proposal_sha256"]),
        approved_by="operator",
    )
    monkeypatch.setattr(
        "outreach.cli.execute_linkedin_followup_send",
        lambda **_kwargs: pytest.fail("duplicate follow-up reached live sender"),
    )

    result = _execute_approved_row(approval)

    assert result["processed_count"] == 0
    assert result["status_counts"] == {"cadence_blocked": 1}
    assert any("repeats" in reason.casefold() for reason in result["cadence_reasons"])

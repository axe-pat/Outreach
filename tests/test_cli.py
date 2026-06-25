from datetime import UTC, date, datetime
import json
from pathlib import Path
from types import SimpleNamespace

from outreach.cli import (
    attach_search_urls_to_candidates,
    apply_linkedin_reconcile_results,
    apply_raw_candidate,
    build_linkedin_company_queue_items,
    build_linkedin_followup_drafts,
    build_linkedin_reconcile_queue_items,
    build_linkedin_message_reconcile_results,
    build_organization_intel_items,
    build_relationship_loop_items,
    build_target_action_queue_items,
    candidate_mentions_company,
    classify_opportunity_action,
    company_search_aliases,
    contact_status_from_invite_result,
    detect_shared_history_signals,
    execute_invite_batch,
    extract_team_size_from_notes,
    extract_tags_from_notes,
    extract_description_from_notes,
    filter_discovered_items,
    fit_band_from_score,
    format_team_size_signal,
    infer_role_bucket,
    infer_fit_reasons,
    item_matches_remote,
    item_matches_tags,
    normalize_tag,
    parse_notes_metadata,
    parse_batch_year,
    parse_team_size_headcount,
    pass_relevance,
    recommend_auto_send_limit,
    resolve_pass_definitions,
    score_opportunity_relevance,
    select_invite_candidates,
    startup_pool_metadata,
    startup_pool_mode,
    startup_pool_send_min_score,
    effective_send_min_score,
    text_contains_signal,
    touchpoint_status_from_invite_result,
)
from outreach.config import OutreachSettings
from outreach.resume_jobs_bridge import CompanyOverride, ResumeJob, build_resume_outreach_queue
from outreach.tracking import ContactRecord, OpportunityRecord, OrganizationRecord, OrganizationType, OutreachWorkbook, SourceKind, TouchpointRecord


def test_tpm_titles_bucket_as_product() -> None:
    settings = OutreachSettings()

    bucket = infer_role_bucket(
        "Principal TPM | Enterprise & Product Security",
        "Principal TPM | Enterprise & Product Security",
        settings,
    )

    assert bucket == "Product"


def test_university_recruiter_gets_separate_bucket() -> None:
    settings = OutreachSettings()

    bucket = infer_role_bucket(
        "Campus Recruiter",
        "Campus Recruiter USC Marshall School of Business Career Center",
        settings,
    )

    assert bucket == "University Recruiting"


def test_solution_engineer_buckets_as_adjacent() -> None:
    settings = OutreachSettings()

    bucket = infer_role_bucket(
        "Senior Solution Engineer at Snowflake",
        "Senior Solution Engineer at Snowflake",
        settings,
    )

    assert bucket == "Adjacent"


def test_founding_mechatronics_engineer_buckets_as_engineering() -> None:
    settings = OutreachSettings()

    bucket = infer_role_bucket(
        "Founding Mechatronics Engineer @ Eden",
        "Founding Mechatronics Engineer @ Eden",
        settings,
    )

    assert bucket == "Engineering"


def test_company_search_aliases_strip_common_startup_suffixes() -> None:
    assert company_search_aliases("Splash Inc.")[:2] == ["Splash Inc.", "Splash"]
    assert "Surtr" in company_search_aliases("Surtr Defense Systems")


def test_candidate_mentions_company_requires_structured_single_word_match() -> None:
    assert candidate_mentions_company(
        SimpleNamespace(title="Founder of bloom | wearebloom.io", snippet="", raw_text=""),
        ["Bloom"],
    )
    assert not candidate_mentions_company(
        SimpleNamespace(title="Engineering Team Lead at Bloomberg", snippet="", raw_text=""),
        ["Bloom"],
    )
    assert not candidate_mentions_company(
        SimpleNamespace(title="Project Manager", snippet="Current: Project Manager at Bloom Energy", raw_text=""),
        ["Bloom"],
    )
    assert not candidate_mentions_company(
        SimpleNamespace(title="Founding Partner at HedgeLegal", snippet="", raw_text=""),
        ["Hedge"],
    )


def test_product_pass_rejects_non_product_noise() -> None:
    assert not pass_relevance(
        "product_usc_marshall",
        "Other",
        "Technology & Strategy leader",
        "Technology & Strategy leader",
    )


def test_engineering_pass_rejects_solution_engineer_noise() -> None:
    assert not pass_relevance(
        "engineering_usc_marshall",
        "Adjacent",
        "Senior Solution Engineer at Snowflake",
        "Senior Solution Engineer at Snowflake",
    )


def test_marshall_passes_disabled_by_default() -> None:
    settings = OutreachSettings()

    assert settings.search.pass_definitions["product_usc_marshall"]["enabled"] is False
    assert settings.search.pass_definitions["engineering_usc_marshall"]["enabled"] is False


def test_broad_fallback_is_small_and_conditional() -> None:
    settings = OutreachSettings()
    broad = settings.search.pass_definitions["broad_fallback"]

    assert broad["limit"] == 6
    assert broad["run_if_below_pool_size"] == 18


def test_enable_marshall_turns_marshall_passes_on() -> None:
    settings = OutreachSettings()

    passes = resolve_pass_definitions(settings, enable_marshall=True)

    assert passes["product_usc_marshall"]["enabled"] is True
    assert passes["engineering_usc_marshall"]["enabled"] is True


def test_include_pass_only_runs_selected_passes() -> None:
    settings = OutreachSettings()

    passes = resolve_pass_definitions(
        settings,
        include_passes=("existing_connections", "product_network"),
    )

    assert passes["existing_connections"]["enabled"] is True
    assert passes["product_network"]["enabled"] is True
    assert passes["product_usc"]["enabled"] is False


def test_force_broad_fallback_removes_pool_gate() -> None:
    settings = OutreachSettings()

    passes = resolve_pass_definitions(settings, force_broad_fallback=True)

    assert passes["broad_fallback"]["enabled"] is True
    assert "run_if_below_pool_size" not in passes["broad_fallback"]


def test_startup_mode_uses_startup_pass_stack() -> None:
    settings = OutreachSettings()

    passes = resolve_pass_definitions(settings, company_mode="startup")

    assert list(passes) == ["existing_connections"]


def test_startup_founder_titles_are_kept_for_startup_mode() -> None:
    assert pass_relevance(
        "startup_founders",
        "Founder",
        "Founder & CEO at Icarus",
        "Founder & CEO at Icarus",
        company_mode="startup",
    )


def test_startup_operator_titles_are_kept_for_startup_mode() -> None:
    assert pass_relevance(
        "startup_operators",
        "Adjacent",
        "Chief of Staff",
        "Chief of Staff",
        company_mode="startup",
    )


def test_startup_broad_fallback_rejects_investors() -> None:
    assert not pass_relevance(
        "startup_company_coverage",
        "Other",
        "Software & AI Investments / Nexus Venture Partners",
        "Software & AI Investments / Nexus Venture Partners",
        company_mode="startup",
    )


def test_startup_company_coverage_keeps_generic_employee_titles() -> None:
    assert pass_relevance(
        "startup_preflight",
        "Other",
        "Member of Technical Staff",
        "Member of Technical Staff",
        company_mode="startup",
    )


def test_startup_preflight_boosts_exact_company_founder_candidates() -> None:
    settings = OutreachSettings()
    deduped: dict[str, dict] = {}

    kept = apply_raw_candidate(
        deduped=deduped,
        raw=SimpleNamespace(
            name="Charles Yong",
            title="Founder @ Vassar Robotics (YC X25)",
            raw_text="Founder @ Vassar Robotics (YC X25)",
            connection_degree="3rd",
            snippet="",
            linkedin_url="https://www.linkedin.com/in/charles/",
            location="",
            subtitle="",
        ),
        company="Vassar Robotics",
        pass_name="startup_preflight",
        pass_config={},
        settings=settings,
        company_mode="startup",
    )

    candidate = deduped["https://www.linkedin.com/in/charles/"]
    assert kept is True
    assert candidate["score"] >= 35
    assert "Startup founder" in candidate["triggers"]


def test_apply_raw_candidate_preserves_shared_history_signals() -> None:
    settings = OutreachSettings()
    deduped: dict[str, dict] = {}

    kept = apply_raw_candidate(
        deduped=deduped,
        raw=SimpleNamespace(
            name="Suman Sundaresh",
            title="Lead Product Manager @ Pebl | Ex-Intuit, Rappi, PayPal",
            raw_text="Lead Product Manager @ Pebl | Ex-Intuit, Rappi, PayPal",
            connection_degree="2nd",
            snippet="Mutual connection",
            linkedin_url="https://www.linkedin.com/in/suman/",
            location="",
            subtitle="",
        ),
        company="Pebl",
        pass_name="startup_preflight",
        pass_config={},
        settings=settings,
        company_mode="startup",
    )

    candidate = deduped["https://www.linkedin.com/in/suman/"]
    assert kept is True
    assert detect_shared_history_signals(candidate["title"], settings) == ["Intuit"]
    assert candidate["shared_history"] is True
    assert candidate["shared_history_signals"] == ["Intuit"]


def test_recommend_auto_send_limit_scales_with_pool_size() -> None:
    assert recommend_auto_send_limit(3) == 0
    assert recommend_auto_send_limit(5) == 5
    assert recommend_auto_send_limit(12) == 10
    assert recommend_auto_send_limit(20) == 12


def test_startup_pool_mode_drives_adaptive_threshold_and_send_cap() -> None:
    assert startup_pool_mode(1) == "micro"
    assert startup_pool_send_min_score("micro") == -5
    assert recommend_auto_send_limit(3, "micro") == 3
    assert startup_pool_mode(8) == "small"
    assert startup_pool_send_min_score("small") == 10
    assert recommend_auto_send_limit(8, "small") == 6


def test_effective_send_min_score_uses_startup_pool_metadata() -> None:
    payload = {
        "company_mode": "startup",
        "startup_pool": {
            "raw_count": 1,
            "kept_count": 1,
            "pool_mode": "micro",
            "adaptive_send_min_score": -5,
        },
        "pass_summaries": [],
    }

    assert effective_send_min_score(payload, requested_min_score=35, adaptive=True) == -5
    assert effective_send_min_score(payload, requested_min_score=35, adaptive=False) == 35


def test_startup_pool_metadata_can_be_recovered_from_older_artifacts() -> None:
    payload = {
        "company_mode": "startup",
        "pass_summaries": [
            {
                "pass_name": "startup_preflight",
                "raw_count": 2,
                "kept_count": 2,
                "coverage_only": True,
            }
        ],
    }

    metadata = startup_pool_metadata(payload)

    assert metadata["pool_mode"] == "micro"
    assert metadata["adaptive_send_min_score"] == -5


def test_sent_without_note_maps_to_sent_tracking_statuses() -> None:
    assert contact_status_from_invite_result("sent_without_note") == "Invited"
    assert touchpoint_status_from_invite_result("sent_without_note") == "Sent"


def test_select_invite_candidates_filters_existing_connections_and_blocked_notes() -> None:
    candidates = [
        {
            "name": "A",
            "linkedin_url": "https://www.linkedin.com/in/a/",
            "existing_connection": False,
            "score": 50,
            "note_qc": {"verdict": "send"},
        },
        {
            "name": "B",
            "linkedin_url": "https://www.linkedin.com/in/b/",
            "existing_connection": True,
            "score": 80,
            "note_qc": {"verdict": "send"},
        },
        {
            "name": "C",
            "linkedin_url": "https://www.linkedin.com/in/c/",
            "existing_connection": False,
            "score": 80,
            "note_qc": {"verdict": "blocked"},
        },
        {
            "name": "D",
            "linkedin_url": "https://www.linkedin.com/in/d/",
            "existing_connection": False,
            "score": 12,
            "note_qc": {"verdict": "send"},
        },
    ]

    selected = select_invite_candidates(candidates, limit=5)

    assert [item["name"] for item in selected] == ["A"]


def test_relative_linkedin_profile_is_treated_as_fallback() -> None:
    settings = OutreachSettings(linkedin_chrome_user_data_dir=Path("playwright/chrome-data"))

    assert settings.using_fallback_linkedin_profile() is True


def test_absolute_linkedin_profile_is_explicit_even_if_it_points_to_outreach_profile() -> None:
    settings = OutreachSettings(linkedin_chrome_user_data_dir=Path.cwd() / "playwright" / "chrome-data")

    assert settings.using_fallback_linkedin_profile() is False
    settings.validate_explicit_linkedin_profile()


def test_execute_invite_batch_persists_progress_per_result(monkeypatch, tmp_path: Path) -> None:
    settings = OutreachSettings(tracking_workspace_dir=tmp_path / "workspace")
    artifact_dir = tmp_path / "artifacts"
    source_artifact = tmp_path / "notes-batch.json"
    source_artifact.write_text("{}")
    persist_calls = []

    monkeypatch.setattr(OutreachSettings, "artifacts_dir", property(lambda self: artifact_dir))

    class _FakeWorkbook:
        def __init__(self, _path: Path) -> None:
            self.path = _path

    monkeypatch.setattr("outreach.cli.OutreachWorkbook", _FakeWorkbook)

    def _fake_persist(**kwargs):
        persist_calls.append(kwargs["processed_candidates"][0]["name"])
        return (1, 1)

    monkeypatch.setattr("outreach.cli.persist_invite_send_results", _fake_persist)

    final_artifact = artifact_dir / "final.json"

    def _fake_write_artifact(_artifacts_dir, _label, payload):
        final_artifact.parent.mkdir(parents=True, exist_ok=True)
        final_artifact.write_text(json.dumps(payload, indent=2))
        return final_artifact

    monkeypatch.setattr("outreach.cli.write_artifact", _fake_write_artifact)

    def _fake_send(self, batch, execute=False, on_result=None):
        results = []
        for candidate in batch:
            result = SimpleNamespace(
                name=candidate["name"],
                linkedin_url=candidate["linkedin_url"],
                status="sent",
                detail="ok",
                note=candidate.get("note", ""),
                screenshot_path="",
            )
            results.append(result)
            if on_result is not None:
                on_result(candidate, result, list(results))
        return results

    monkeypatch.setattr("outreach.cli.LinkedInScraper.send_connection_requests", _fake_send)

    batch = [
        {"name": "Alice", "linkedin_url": "https://www.linkedin.com/in/alice/", "note": "hi"},
        {"name": "Bob", "linkedin_url": "https://www.linkedin.com/in/bob/", "note": "hello"},
    ]

    artifact, progress_artifact, status_counts, contacts_added, touchpoints_added = execute_invite_batch(
        settings=settings,
        company="Scale AI",
        source_artifact_path=source_artifact,
        batch=batch,
        execute=True,
        limit=2,
        start_at=0,
        verdict="send",
        min_score=35,
    )

    assert artifact == final_artifact
    assert progress_artifact.exists()
    progress_payload = json.loads(progress_artifact.read_text())
    assert progress_payload["count"] == 2
    assert [item["name"] for item in progress_payload["results"]] == ["Alice", "Bob"]
    assert persist_calls == ["Alice", "Bob"]
    assert status_counts == {"sent": 2}
    assert contacts_added == 2
    assert touchpoints_added == 2


def test_execute_invite_batch_dry_run_does_not_persist(monkeypatch, tmp_path: Path) -> None:
    settings = OutreachSettings(tracking_workspace_dir=tmp_path / "workspace")
    artifact_dir = tmp_path / "artifacts"
    source_artifact = tmp_path / "notes-batch.json"
    source_artifact.write_text("{}")
    persist_calls = []

    monkeypatch.setattr(OutreachSettings, "artifacts_dir", property(lambda self: artifact_dir))
    monkeypatch.setattr("outreach.cli.OutreachWorkbook", lambda _path: object())

    def _fake_persist(**kwargs):
        persist_calls.append(kwargs)
        return (1, 1)

    monkeypatch.setattr("outreach.cli.persist_invite_send_results", _fake_persist)

    final_artifact = artifact_dir / "final.json"

    def _fake_write_artifact(_artifacts_dir, _label, payload):
        final_artifact.parent.mkdir(parents=True, exist_ok=True)
        final_artifact.write_text(json.dumps(payload, indent=2))
        return final_artifact

    monkeypatch.setattr("outreach.cli.write_artifact", _fake_write_artifact)

    def _fake_send(self, batch, execute=False, on_result=None):
        results = []
        for candidate in batch:
            result = SimpleNamespace(
                name=candidate["name"],
                linkedin_url=candidate["linkedin_url"],
                status="dry_run_ready",
                detail="ok",
                note=candidate.get("note", ""),
                screenshot_path="",
            )
            results.append(result)
            if on_result is not None:
                on_result(candidate, result, list(results))
        return results

    monkeypatch.setattr("outreach.cli.LinkedInScraper.send_connection_requests", _fake_send)

    _, progress_artifact, status_counts, contacts_added, touchpoints_added = execute_invite_batch(
        settings=settings,
        company="Tasklet",
        source_artifact_path=source_artifact,
        batch=[{"name": "Alice", "linkedin_url": "https://www.linkedin.com/in/alice/", "note": "hi"}],
        execute=False,
        limit=1,
        start_at=0,
        verdict="send",
        min_score=35,
    )

    assert progress_artifact.exists()
    assert persist_calls == []
    assert status_counts == {"dry_run_ready": 1}
    assert contacts_added == 0
    assert touchpoints_added == 0


def test_build_linkedin_reconcile_queue_selects_stale_invites() -> None:
    organization = OrganizationRecord(
        organization_id="org-snyk",
        name="Snyk",
        organization_type=OrganizationType.COMPANY,
    )
    stale_contact = ContactRecord(
        contact_id="ct-stale",
        organization_id="org-snyk",
        full_name="Mehak Singh",
        status="Invited",
        linkedin_url="https://www.linkedin.com/in/mehak/",
        last_contacted_at="2026-06-24T08:00:00+00:00",
    )
    fresh_contact = ContactRecord(
        contact_id="ct-fresh",
        organization_id="org-snyk",
        full_name="Fresh Invite",
        status="Invited",
        linkedin_url="https://www.linkedin.com/in/fresh/",
        last_contacted_at="2026-06-25T10:30:00+00:00",
    )
    connected_contact = ContactRecord(
        contact_id="ct-connected",
        organization_id="org-snyk",
        full_name="Already Connected",
        status="Connected",
        linkedin_url="https://www.linkedin.com/in/connected/",
        last_contacted_at="2026-06-24T08:00:00+00:00",
    )
    invite_touchpoint = TouchpointRecord(
        touchpoint_id="tp-stale",
        organization_id="org-snyk",
        contact_id="ct-stale",
        status="Sent",
        message_kind="linkedin_invite",
        message_text="Hi Mehak, would value a referral pointer.",
        sent_at="2026-06-24T08:00:00+00:00",
    )

    items = build_linkedin_reconcile_queue_items(
        organizations=[organization],
        contacts=[stale_contact, fresh_contact, connected_contact],
        touchpoints=[invite_touchpoint],
        min_age_hours=12,
        max_age_days=14,
        now=datetime(2026, 6, 25, 12, 0, tzinfo=UTC),
    )

    assert [item["contact_id"] for item in items] == ["ct-stale"]
    assert items[0]["company"] == "Snyk"
    assert items[0]["original_invite_note"] == "Hi Mehak, would value a referral pointer."


def test_apply_linkedin_reconcile_results_updates_contacts_and_touchpoints(tmp_path: Path) -> None:
    workbook = OutreachWorkbook(tmp_path / "workspace")
    workbook.upsert_organization(
        OrganizationRecord(
            organization_id="org-workwhile",
            name="WorkWhile",
            organization_type=OrganizationType.STARTUP,
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-roshni",
            organization_id="org-workwhile",
            full_name="Roshni Ramakrishnan",
            status="Invited",
            linkedin_url="https://www.linkedin.com/in/roshni/",
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-owen",
            organization_id="org-workwhile",
            full_name="Owen Crook",
            status="Invited",
            linkedin_url="https://www.linkedin.com/in/owen/",
        )
    )
    workbook.upsert_contact(
        ContactRecord(
            contact_id="ct-deepanshu",
            organization_id="org-workwhile",
            full_name="Deepanshu Johar",
            status="Invited",
            linkedin_url="https://www.linkedin.com/in/deepanshu/",
        )
    )

    result = apply_linkedin_reconcile_results(
        workbook=workbook,
        results=[
            {
                "contact_id": "ct-roshni",
                "name": "Roshni Ramakrishnan",
                "linkedin_url": "https://www.linkedin.com/in/roshni/",
                "status": "connected",
                "detail": "Profile shows Message.",
            },
            {
                "contact_id": "ct-owen",
                "name": "Owen Crook",
                "linkedin_url": "https://www.linkedin.com/in/owen/",
                "status": "replied",
                "message_text": "Happy to help. What role are you looking at?",
            },
            {
                "contact_id": "ct-deepanshu",
                "name": "Deepanshu Johar",
                "linkedin_url": "https://www.linkedin.com/in/deepanshu/",
                "status": "connected",
                "latest_message": "You sent an attachment",
                "last_sender": "",
            },
        ],
        source_artifact="artifacts/reconcile.json",
        apply_changes=True,
    )

    assert result["summary"]["connected"] == 2
    assert result["summary"]["replied"] == 1
    assert result["summary"]["updated_contacts"] == 3
    assert result["summary"]["touchpoints_added"] == 3
    contacts = {item.contact_id: item for item in workbook.list_contacts()}
    assert contacts["ct-roshni"].status == "Connected"
    assert contacts["ct-owen"].status == "Replied"
    assert contacts["ct-deepanshu"].status == "Connected"
    deepanshu_result = next(item for item in result["results"] if item["contact_id"] == "ct-deepanshu")
    assert deepanshu_result["needs_follow_up"] is False
    touchpoints = workbook.list_touchpoints()
    assert {item.status for item in touchpoints} == {"Accepted", "Replied"}
    assert any(item.message_kind == "linkedin_reply" for item in touchpoints)


def test_build_linkedin_message_reconcile_results_uses_thread_offset() -> None:
    contacts = [
        ContactRecord(
            contact_id="ct-roshni",
            organization_id="org-workwhile",
            full_name="Roshni Ramakrishnan",
            status="Invited",
            linkedin_url="https://www.linkedin.com/in/roshni/",
        ),
        ContactRecord(
            contact_id="ct-owen",
            organization_id="org-workwhile",
            full_name="Owen Crook",
            status="Invited",
            linkedin_url="https://www.linkedin.com/in/owen/",
        ),
        ContactRecord(
            contact_id="ct-old",
            organization_id="org-workwhile",
            full_name="Old Thread",
            status="Invited",
            linkedin_url="https://www.linkedin.com/in/old/",
        ),
        ContactRecord(
            contact_id="ct-shubhankit",
            organization_id="org-d-matrix",
            full_name="Shubhankit R.",
            status="Invited",
            linkedin_url="https://www.linkedin.com/in/shubhankitr/",
        ),
    ]
    touchpoints = [
        TouchpointRecord(
            touchpoint_id="tp-roshni",
            organization_id="org-workwhile",
            contact_id="ct-roshni",
            status="Sent",
            message_kind="linkedin_invite",
            message_text="Hi Roshni, I'd value a pointer on how technical PM candidates can stand out.",
        ),
        TouchpointRecord(
            touchpoint_id="tp-owen",
            organization_id="org-workwhile",
            contact_id="ct-owen",
            status="Sent",
            message_kind="linkedin_invite",
            message_text="Hi Owen, would love a quick pointer on how builders work with product there.",
        ),
        TouchpointRecord(
            touchpoint_id="tp-shubhankit",
            organization_id="org-d-matrix",
            contact_id="ct-shubhankit",
            status="Sent",
            message_kind="linkedin_invite",
            message_text="Hi Shubhankit, I'm exploring PM roles at d-Matrix.",
        ),
    ]
    threads = [
        {
            "thread_id": "thread-roshni",
            "name": "Roshni Ramakrishnan",
            "thread_url": "https://www.linkedin.com/messaging/thread/thread-roshni/",
            "latest_message": "You are now connected",
            "last_sender": "",
        },
        {
            "thread_id": "thread-owen",
            "name": "Owen Crook",
            "thread_url": "https://www.linkedin.com/messaging/thread/thread-owen/",
            "latest_message": "Happy to help. What role are you looking at?",
            "last_sender": "Owen Crook",
            "unread": True,
        },
        {
            "thread_id": "thread-old",
            "name": "Old Thread",
            "thread_url": "https://www.linkedin.com/messaging/thread/thread-old/",
            "latest_message": "Already seen",
            "last_sender": "Old Thread",
        },
        {
            "thread_id": "thread-shubhankit",
            "name": "Shubhankit Rathore",
            "thread_url": "",
            "latest_message": "Hi Shubhankit, I'm exploring PM roles at d-Matrix.",
            "last_sender": "You",
        },
    ]

    results, next_state = build_linkedin_message_reconcile_results(
        threads=threads,
        contacts=contacts,
        touchpoints=touchpoints,
        state={"seen_thread_ids": ["thread-old"]},
    )

    assert [item["contact_id"] for item in results] == ["ct-roshni", "ct-owen", "ct-shubhankit"]
    assert [item["status"] for item in results] == ["connected", "replied", "connected"]
    assert set(next_state["seen_thread_ids"]) == {"thread-roshni", "thread-owen", "thread-old", "thread-shubhankit"}


def test_build_linkedin_followup_drafts_handles_accepts_and_replies() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-snyk",
            name="Snyk",
            organization_type=OrganizationType.COMPANY,
        ),
        OrganizationRecord(
            organization_id="org-sortly",
            name="Sortly",
            organization_type=OrganizationType.COMPANY,
        ),
        OrganizationRecord(
            organization_id="org-voker",
            name="Voker",
            organization_type=OrganizationType.STARTUP,
            notes="description=Voker is the Agent Analytics Platform for monitoring and improving your AI agents.",
        ),
    ]
    contacts = [
        ContactRecord(
            contact_id="ct-mehak",
            organization_id="org-snyk",
            full_name="Mehak Singh",
            title="Associate Software Engineer at Snyk",
            contact_type="Engineering",
        ),
        ContactRecord(
            contact_id="ct-deepanshu",
            organization_id="org-sortly",
            full_name="Deepanshu Johar",
            title="SWE 2 @ Sortly",
            contact_type="Engineering",
        ),
        ContactRecord(
            contact_id="ct-hamid",
            organization_id="org-snyk",
            full_name="Hamid Example",
            title="Software Engineer",
            contact_type="Engineering",
        ),
        ContactRecord(
            contact_id="ct-david",
            organization_id="org-snyk",
            full_name="David Alessi",
            title="Product @ Snyk | AI, Data Infrastructure, ex-Microsoft",
            contact_type="Engineering",
        ),
        ContactRecord(
            contact_id="ct-tyler",
            organization_id="org-voker",
            full_name="Tyler Postle",
            title="CEO @ Voker (YC S24): AI Founder & Operator",
            contact_type="Founder",
        ),
    ]

    drafts = build_linkedin_followup_drafts(
        reconcile_results=[
            {
                "contact_id": "ct-mehak",
                "organization_id": "org-snyk",
                "name": "Mehak Singh",
                "normalized_status": "connected",
                "needs_follow_up": True,
                "original_invite_note": "Would really value a referral or pointer on how to stand out to the hiring team.",
            },
            {
                "contact_id": "ct-deepanshu",
                "organization_id": "org-sortly",
                "name": "Deepanshu Johar",
                "normalized_status": "replied",
                "latest_message": "I can share your profile to the HR in case that helps ?",
            },
            {
                "contact_id": "ct-hamid",
                "organization_id": "org-snyk",
                "name": "Hamid Example",
                "normalized_status": "connected",
                "needs_follow_up": True,
                "original_invite_note": "I'm exploring PM roles and would love to learn from your experience.",
            },
            {
                "contact_id": "ct-david",
                "organization_id": "org-snyk",
                "name": "David Alessi",
                "normalized_status": "connected",
                "needs_follow_up": True,
                "original_invite_note": "Would love to connect and learn how engineering-heavy product work gets shaped.",
            },
            {
                "contact_id": "ct-tyler",
                "organization_id": "org-voker",
                "name": "Tyler Postle",
                "normalized_status": "connected",
                "needs_follow_up": True,
                "original_invite_note": "Would love to connect and learn from your experience.",
            },
        ],
        organizations=organizations,
        contacts=contacts,
    )

    assert [item["draft_kind"] for item in drafts] == [
        "accepted_follow_up",
        "referral_offer_reply",
        "accepted_follow_up",
        "accepted_follow_up",
        "accepted_follow_up",
    ]
    assert "referral" in str(drafts[0]["draft_message"]).lower()
    assert "short blurb" in str(drafts[1]["draft_message"]).lower()
    assert drafts[0]["company"] == "Snyk"
    assert drafts[0]["original_invite_note"].startswith("Would really value")
    assert drafts[1]["latest_message"].startswith("I can share your profile")
    assert "referral path or hiring contact" in str(drafts[2]["draft_message"])
    assert drafts[3]["followup_audience"] == "product"
    assert "would love your perspective" in str(drafts[3]["draft_message"]).lower()
    assert "could translate to the product work there" in str(drafts[3]["draft_message"])
    assert "product or recruiting person" not in str(drafts[3]["draft_message"]).lower()
    assert drafts[4]["followup_audience"] == "founder"
    assert "AI agent analytics work" in str(drafts[4]["draft_message"])
    assert "could translate to what the team is building" in str(drafts[4]["draft_message"])
    assert "happy to share more context if useful" in str(drafts[4]["draft_message"])


def test_attach_search_urls_to_candidates_uses_first_matching_pass() -> None:
    payload = {
        "pass_summaries": [
            {"pass_name": "product_network", "final_url": "https://www.linkedin.com/search/results/people/?foo=1"},
            {"pass_name": "engineering_usc", "final_url": "https://www.linkedin.com/search/results/people/?bar=2"},
        ]
    }
    candidates = [
        {"name": "Alice", "passes": ["product_network", "engineering_usc"]},
        {"name": "Bob", "passes": ["missing_pass"]},
    ]

    enriched = attach_search_urls_to_candidates(payload, candidates)

    assert enriched[0]["_search_url"] == "https://www.linkedin.com/search/results/people/?foo=1"
    assert "_search_url" not in enriched[1]


def test_parse_team_size_headcount_handles_commas() -> None:
    assert parse_team_size_headcount("1,600 employees") == 1600


def test_parse_batch_year_extracts_year() -> None:
    assert parse_batch_year("S2024") == 2024
    assert parse_batch_year("Spring 2026") == 2026


def test_normalize_tag_handles_dash_and_spacing() -> None:
    assert normalize_tag("Generative-AI") == "generative ai"


def test_item_matches_remote_uses_company_and_opportunity_location() -> None:
    assert item_matches_remote({"location": "Fully Remote", "opportunities": []}) is True
    assert item_matches_remote({"location": "Los Angeles", "opportunities": [{"location": "Remote"}]}) is True
    assert item_matches_remote({"location": "Los Angeles", "opportunities": []}) is False


def test_item_matches_tags_supports_partial_match() -> None:
    item = {"tags": ["artificial intelligence", "robotics"]}

    assert item_matches_tags(item, ("ai",)) is False
    assert item_matches_tags(item, ("robot",)) is True
    assert item_matches_tags(item, ("artificial-intelligence",)) is True


def test_extract_team_size_from_notes_reads_discovery_note() -> None:
    assert extract_team_size_from_notes("batch=Spring 2026 | team_size=12 employees | tags=ai") == 12


def test_parse_notes_metadata_extracts_structured_fields() -> None:
    notes = "batch=Spring 2026 | founded_year=2024 | tags=ai,robotics | description=Builds AI systems"

    metadata = parse_notes_metadata(notes)

    assert metadata["batch"] == "Spring 2026"
    assert metadata["founded_year"] == "2024"
    assert extract_tags_from_notes(notes) == ["ai", "robotics"]
    assert extract_description_from_notes(notes) == "Builds AI systems"


def test_text_contains_signal_avoids_short_keyword_false_positive() -> None:
    assert text_contains_signal("autonomous aircraft platform", "ai") is False
    assert text_contains_signal("artificial intelligence platform", "ai") is False
    assert text_contains_signal("ai platform", "ai") is True


def test_format_team_size_signal_adds_employee_suffix_for_bare_numbers() -> None:
    assert format_team_size_signal("16") == "16 employees"
    assert format_team_size_signal("150 Employees") == "150 Employees"


def test_filter_discovered_items_applies_jobs_size_and_batch_filters() -> None:
    items = [
        {"organization_name": "OlderCo", "jobs_url": "", "team_size": "50 employees", "batch": "W2020"},
        {"organization_name": "HiringCo", "jobs_url": "https://example.com/jobs", "team_size": "12 employees", "batch": "S2025"},
        {"organization_name": "BigCo", "jobs_url": "https://example.com/jobs", "team_size": "500 employees", "batch": "S2025"},
    ]

    filtered = filter_discovered_items(
        items,
        require_jobs_url=True,
        max_team_size=100,
        min_batch_year=2024,
    )

    assert [item["organization_name"] for item in filtered] == ["HiringCo"]


def test_filter_discovered_items_applies_remote_and_tag_filters() -> None:
    items = [
        {
            "organization_name": "RemoteAI",
            "jobs_url": "https://example.com/jobs",
            "team_size": "20 employees",
            "batch": "S2025",
            "location": "Fully Remote",
            "tags": ["artificial intelligence"],
            "opportunities": [],
        },
        {
            "organization_name": "OfficeRobotics",
            "jobs_url": "https://example.com/jobs",
            "team_size": "20 employees",
            "batch": "S2025",
            "location": "Los Angeles",
            "tags": ["robotics"],
            "opportunities": [],
        },
    ]

    filtered = filter_discovered_items(
        items,
        require_jobs_url=True,
        max_team_size=100,
        min_batch_year=2024,
        remote_only=True,
        include_tags=("artificial intelligence",),
    )

    assert [item["organization_name"] for item in filtered] == ["RemoteAI"]


def test_build_linkedin_company_queue_prioritizes_unworked_hiring_startups() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-mount",
            name="Mount",
            organization_type=OrganizationType.STARTUP,
            target_lists="yc;startup;sf;hiring",
            status="Researching",
            source_kind=SourceKind.YC_DIRECTORY,
            notes="batch=Spring 2026 | team_size=2 employees | tags=insurance,ai",
        ),
        OrganizationRecord(
            organization_id="org-doodle",
            name="Doodle Labs",
            organization_type=OrganizationType.COMPANY,
            target_lists="built_in;la;companies",
            status="Researching",
            source_kind=SourceKind.STARTUP_DIRECTORY,
            notes="team_size=50 Employees | tags=robotics",
        ),
    ]
    opportunities = [
        OpportunityRecord(
            opportunity_id="opp-mount",
            organization_id="org-mount",
            title="Founding AI Engineer",
        ),
        OpportunityRecord(
            opportunity_id="opp-doodle",
            organization_id="org-doodle",
            title="Marketing Designer",
        ),
    ]
    contacts = [
        ContactRecord(
            contact_id="ct-doodle",
            organization_id="org-doodle",
            full_name="Existing Contact",
        )
    ]
    touchpoints = [
        TouchpointRecord(
            touchpoint_id="tp-doodle",
            organization_id="org-doodle",
            message_text="hello",
        )
    ]

    queue = build_linkedin_company_queue_items(
        organizations=organizations,
        opportunities=opportunities,
        contacts=contacts,
        touchpoints=touchpoints,
        require_no_contacts=False,
        require_hiring_signal=True,
    )

    assert queue[0].company == "Mount"
    assert queue[0].company_mode == "startup"
    assert "No LinkedIn-sourced contacts yet" in queue[0].triggers


def test_build_linkedin_company_queue_filters_target_lists_and_contacts() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-1",
            name="One",
            organization_type=OrganizationType.STARTUP,
            target_lists="yc;startup",
            source_kind=SourceKind.YC_DIRECTORY,
        ),
        OrganizationRecord(
            organization_id="org-2",
            name="Two",
            organization_type=OrganizationType.COMPANY,
            target_lists="built_in;la",
            source_kind=SourceKind.STARTUP_DIRECTORY,
        ),
    ]
    opportunities = [
        OpportunityRecord(opportunity_id="opp-1", organization_id="org-1", title="Role A"),
        OpportunityRecord(opportunity_id="opp-2", organization_id="org-2", title="Role B"),
    ]
    contacts = [ContactRecord(contact_id="ct-2", organization_id="org-2", full_name="Person")]

    queue = build_linkedin_company_queue_items(
        organizations=organizations,
        opportunities=opportunities,
        contacts=contacts,
        touchpoints=[],
        include_target_lists=("yc",),
        require_no_contacts=True,
        require_hiring_signal=True,
    )

    assert [item.company for item in queue] == ["One"]


def test_build_linkedin_company_queue_keeps_non_linkedin_contacts_eligible() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-1",
            name="Mount",
            organization_type=OrganizationType.STARTUP,
            target_lists="yc;startup",
            source_kind=SourceKind.YC_DIRECTORY,
        )
    ]
    opportunities = [OpportunityRecord(opportunity_id="opp-1", organization_id="org-1", title="Role A")]
    contacts = [
        ContactRecord(
            contact_id="ct-1",
            organization_id="org-1",
            full_name="Founder",
            source_kind=SourceKind.YC_DIRECTORY,
        )
    ]

    queue = build_linkedin_company_queue_items(
        organizations=organizations,
        opportunities=opportunities,
        contacts=contacts,
        touchpoints=[],
        require_no_contacts=True,
        require_hiring_signal=True,
    )

    assert [item.company for item in queue] == ["Mount"]
    assert queue[0].linkedin_contact_count == 0


def test_build_linkedin_company_queue_mode_infers_big_company() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-1",
            name="BigCo",
            organization_type=OrganizationType.COMPANY,
            target_lists="built_in",
            source_kind=SourceKind.STARTUP_DIRECTORY,
            notes="team_size=5000 Employees",
        )
    ]
    opportunities = [OpportunityRecord(opportunity_id="opp-1", organization_id="org-1", title="Role A")]

    queue = build_linkedin_company_queue_items(
        organizations=organizations,
        opportunities=opportunities,
        contacts=[],
        touchpoints=[],
        require_no_contacts=True,
        require_hiring_signal=True,
    )

    assert queue[0].company_mode == "big_company"


def test_infer_fit_reasons_scores_ai_startup_hiring_signals() -> None:
    organization = OrganizationRecord(
        organization_id="org-mount",
        name="Mount",
        organization_type=OrganizationType.STARTUP,
        city="San Francisco",
        notes="team_size=12 employees | location=San Francisco, CA | tags=ai,insurance | description=AI risk platform",
    )
    opportunities = [OpportunityRecord(opportunity_id="opp-1", organization_id="org-mount", title="Product Strategy Intern")]

    score, reasons = infer_fit_reasons(
        organization=organization,
        tags=["ai", "insurance"],
        description="AI risk platform for autonomous agents",
        opportunities=opportunities,
    )

    assert score >= 60
    assert "AI/ML angle" in reasons
    assert fit_band_from_score(score) == "strong"


def test_build_organization_intel_items_shapes_reviewable_output() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-1",
            name="Alpha",
            organization_type=OrganizationType.STARTUP,
            target_lists="yc;startup",
            city="San Francisco",
            source_kind=SourceKind.YC_DIRECTORY,
            discovered_at="2026-04-09T10:00:00+00:00",
            website="https://alpha.example.com",
            source_url="https://www.ycombinator.com/companies/alpha",
            notes=(
                "batch=Spring 2026 | founded_year=2024 | team_size=20 employees | "
                "location=San Francisco, CA | jobs_count=2 | tags=ai,data | "
                "description=Builds AI data workflow software"
            ),
        )
    ]
    opportunities = [
        OpportunityRecord(
            opportunity_id="opp-1",
            organization_id="org-1",
            title="Product Operations Intern",
        )
    ]
    contacts = [ContactRecord(contact_id="ct-1", organization_id="org-1", full_name="Founder", contact_type="founder")]
    touchpoints: list[TouchpointRecord] = []

    items = build_organization_intel_items(
        organizations=organizations,
        opportunities=opportunities,
        contacts=contacts,
        touchpoints=touchpoints,
        require_hiring_signal=True,
    )

    assert len(items) == 1
    assert items[0]["company"] == "Alpha"
    assert items[0]["public_revenue_signal"] == "Not surfaced in the source pages yet."
    assert items[0]["fit_band"] == "strong"
    assert "AI/ML angle" in items[0]["fit_reasons"]


def test_score_opportunity_relevance_prefers_pm_intern_over_engineering() -> None:
    organization = OrganizationRecord(
        organization_id="org-1",
        name="Alpha",
        organization_type=OrganizationType.STARTUP,
    )

    product_score, product_reasons = score_opportunity_relevance("MBA Product Manager Intern", organization)
    engineer_score, engineer_reasons = score_opportunity_relevance("Senior Software Engineer", organization)

    assert product_score >= 80
    assert "Product role" in product_reasons
    assert engineer_score == 0
    assert engineer_reasons == ["Role looks functionally off-target"]
    assert classify_opportunity_action(product_score) == "apply_now"


def test_build_target_action_queue_distinguishes_apply_vs_outreach() -> None:
    organizations = [
        OrganizationRecord(
            organization_id="org-apply",
            name="ApplyCo",
            organization_type=OrganizationType.STARTUP,
            target_lists="yc;startup",
            notes="team_size=20 employees | location=San Francisco | tags=ai | description=AI workflow platform",
        ),
        OrganizationRecord(
            organization_id="org-outreach",
            name="OutreachCo",
            organization_type=OrganizationType.STARTUP,
            target_lists="yc;startup",
            notes="team_size=8 employees | location=Los Angeles | tags=robotics | description=Robotics platform for logistics",
        ),
    ]
    opportunities = [
        OpportunityRecord(
            opportunity_id="opp-1",
            organization_id="org-apply",
            title="Product Operations Intern",
        ),
        OpportunityRecord(
            opportunity_id="opp-2",
            organization_id="org-outreach",
            title="Senior Mechanical Engineer",
        ),
    ]
    contacts = [
        ContactRecord(
            contact_id="ct-1",
            organization_id="org-outreach",
            full_name="Founder One",
            contact_type="founder",
        )
    ]

    items = build_target_action_queue_items(
        organizations=organizations,
        opportunities=opportunities,
        contacts=contacts,
        touchpoints=[],
        include_target_lists=("yc",),
    )

    assert items[0]["company"] == "ApplyCo"
    assert items[0]["action"] == "apply_now"
    outreach_item = next(item for item in items if item["company"] == "OutreachCo")
    assert outreach_item["action"] == "outreach_now"
    assert outreach_item["relevant_role_count"] == 0


def test_relationship_loop_follow_up_connected_contact() -> None:
    organization = OrganizationRecord(
        organization_id="org-synphony",
        name="Synphony",
        organization_type=OrganizationType.STARTUP,
        target_lists="yc;startup;hiring",
        notes="team_size=12 | tags=robotics,ai,data | description=Robotics automation platform with a data pipeline for physical AI.",
    )
    contact = ContactRecord(
        contact_id="ct-sean",
        organization_id="org-synphony",
        full_name="Sean Wu",
        title="CEO",
        contact_type="Founder",
        status="Connected",
        last_contacted_at="2026-06-24T10:00:00+00:00",
    )
    touchpoint = TouchpointRecord(
        touchpoint_id="tp-1",
        organization_id="org-synphony",
        contact_id="ct-sean",
        status="Sent",
        message_kind="linkedin_invite",
        message_text="Hi Sean, I'm a Marshall MBA exploring product/operator paths at Synphony.",
        sent_at="2026-06-24T10:00:00+00:00",
    )

    items = build_relationship_loop_items(
        organizations=[organization],
        opportunities=[],
        contacts=[contact],
        touchpoints=[touchpoint],
        now=datetime(2026, 6, 25, tzinfo=UTC),
    )

    assert items[0]["company"] == "Synphony"
    assert items[0]["relationship_stage"] == "connected_no_conversation"
    assert items[0]["next_action"] == "follow_up_connected_contact"
    assert items[0]["suggested_contact_name"] == "Sean Wu"
    assert "thanks for connecting" in str(items[0]["suggested_message"])
    assert "Synphony" in str(items[0]["suggested_message"])


def test_relationship_loop_runs_people_search_when_core_company_has_no_contacts() -> None:
    organization = OrganizationRecord(
        organization_id="org-mount",
        name="Mount",
        organization_type=OrganizationType.STARTUP,
        target_lists="yc;startup;hiring",
        notes="team_size=2 | tags=insurance,ai,security | description=Insurance and risk evaluation for AI agents.",
    )
    opportunity = OpportunityRecord(
        opportunity_id="opp-1",
        organization_id="org-mount",
        title="Product Management Intern",
    )

    items = build_relationship_loop_items(
        organizations=[organization],
        opportunities=[opportunity],
        contacts=[],
        touchpoints=[],
        now=datetime(2026, 6, 25, tzinfo=UTC),
    )

    assert items[0]["relationship_stage"] == "unstarted"
    assert items[0]["next_action"] == "run_linkedin_people_search"
    assert items[0]["relationship_goal"] == "summer_fall_internship"
    assert items[0]["relationship_gap"] == 3


def test_relationship_loop_adds_channel_after_stale_linkedin_wave() -> None:
    organization = OrganizationRecord(
        organization_id="org-workwhile",
        name="WorkWhile",
        organization_type=OrganizationType.STARTUP,
        target_lists="startup;hiring;priority",
        notes="team_size=90 | tags=marketplace,platform | description=Labor marketplace platform for hourly work.",
    )
    contacts = [
        ContactRecord(
            contact_id=f"ct-{index}",
            organization_id="org-workwhile",
            full_name=f"Engineer {index}",
            contact_type="Engineering",
            status="Invited",
            last_contacted_at="2026-06-18T10:00:00+00:00",
        )
        for index in range(10)
    ]
    touchpoints = [
        TouchpointRecord(
            touchpoint_id=f"tp-{index}",
            organization_id="org-workwhile",
            contact_id=f"ct-{index}",
            status="Sent",
            message_kind="linkedin_invite",
            message_text=f"Invite {index}",
            sent_at="2026-06-18T10:00:00+00:00",
        )
        for index in range(10)
    ]

    items = build_relationship_loop_items(
        organizations=[organization],
        opportunities=[],
        contacts=contacts,
        touchpoints=touchpoints,
        outreach_wave_size=10,
        now=datetime(2026, 6, 25, tzinfo=UTC),
    )

    assert items[0]["relationship_stage"] == "outreach_sent"
    assert items[0]["next_action"] == "research_email_path"
    assert items[0]["sent_invite_count"] == 10


def test_relationship_loop_does_not_treat_generic_startup_tag_as_core() -> None:
    organization = OrganizationRecord(
        organization_id="org-low-fit",
        name="LowFit Startup",
        organization_type=OrganizationType.STARTUP,
        target_lists="startup;hiring",
        notes="team_size=30 | tags=consumer,social | description=Consumer social events app.",
    )

    items = build_relationship_loop_items(
        organizations=[organization],
        opportunities=[],
        contacts=[],
        touchpoints=[],
        min_fit_score=70,
        now=datetime(2026, 6, 25, tzinfo=UTC),
    )

    assert items[0]["next_action"] == "watch"
    assert "not strong enough" in str(items[0]["action_reason"])


def test_build_resume_outreach_queue_applies_override_bias_and_company_cap() -> None:
    jobs = [
        ResumeJob(
            row_id="1",
            company="Typeface",
            role_title="Product Manager Intern",
            location="San Francisco",
            url="https://example.com/typeface-1",
            url_hash="hash-1",
            source="linkedin_live_jobs_v1",
            status="queued",
            normalized_status="queued",
            fit_score=7.4,
            fit_rationale="good fit",
            date_found=date.today(),
            date_posted_raw="",
            folder_path="",
            jd_text="",
            notes="",
        ),
        ResumeJob(
            row_id="2",
            company="Typeface",
            role_title="PM Intern 2",
            location="San Francisco",
            url="https://example.com/typeface-2",
            url_hash="hash-2",
            source="linkedin_live_jobs_v1",
            status="queued",
            normalized_status="queued",
            fit_score=7.2,
            fit_rationale="good fit",
            date_found=date.today(),
            date_posted_raw="",
            folder_path="",
            jd_text="",
            notes="",
        ),
        ResumeJob(
            row_id="3",
            company="Typeface",
            role_title="PM Intern 3",
            location="San Francisco",
            url="https://example.com/typeface-3",
            url_hash="hash-3",
            source="linkedin_live_jobs_v1",
            status="queued",
            normalized_status="queued",
            fit_score=7.1,
            fit_rationale="good fit",
            date_found=date.today(),
            date_posted_raw="",
            folder_path="",
            jd_text="",
            notes="",
        ),
        ResumeJob(
            row_id="4",
            company="TikTok",
            role_title="Product Manager Intern",
            location="San Jose",
            url="https://example.com/tiktok",
            url_hash="hash-4",
            source="linkedin_live_jobs_v1",
            status="generated",
            normalized_status="generated",
            fit_score=8.6,
            fit_rationale="excellent fit",
            date_found=date.today(),
            date_posted_raw="",
            folder_path="",
            jd_text="",
            notes="",
        ),
    ]
    overrides = {
        "typeface": CompanyOverride(
            company="Typeface",
            normalized_company="typeface",
            company_type_override="startup",
            startup_bias="high",
            notes="High outreach value",
        ),
        "tiktok": CompanyOverride(
            company="TikTok",
            normalized_company="tiktok",
            company_type_override="big_company",
            startup_bias="deprioritize",
            notes="Prefer startups first",
        ),
    }

    queue = build_resume_outreach_queue(jobs, company_overrides=overrides, max_per_company=2)

    assert len(queue) == 3
    assert queue[0].company == "Typeface"
    assert queue[0].company_type == "startup"
    assert queue[0].startup_bias == "high"
    assert queue[-1].company == "TikTok"
    assert queue[-1].startup_bias == "deprioritize"

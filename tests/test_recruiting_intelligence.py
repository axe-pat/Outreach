import csv
import json
import os
from pathlib import Path

from outreach.recruiting_intelligence import (
    company_signals_from_feed_ledger,
    company_signals_from_source_metrics,
    role_inputs_from_source_metrics,
)
from outreach.role_surface_monitor import RoleStage, SourceRunStatus


def test_feed_ledger_becomes_scored_company_signal(tmp_path: Path) -> None:
    path = tmp_path / "feed.csv"
    fields = ["company", "signal_kinds", "review_disposition", "post_text", "post_url", "author_name", "last_seen_at", "company_url", "context", "relevance_reason"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({"company": "Promising AI", "signal_kinds": "company_discovery;startup_discovery;hiring;funding", "review_disposition": "pending", "post_text": "Hiring product and strategy for an AI data workflow startup", "post_url": "https://linkedin.test/post/1"})

    signals = company_signals_from_feed_ledger(path, run_id="run-1")

    assert len(signals) == 1
    assert signals[0].company_name == "Promising AI"
    assert signals[0].rubric.total >= 10
    assert signals[0].provenance[0].source_type == "linkedin_home_feed"


def test_feed_company_page_does_not_turn_person_author_into_company(tmp_path: Path) -> None:
    path = tmp_path / "feed.csv"
    fields = [
        "company",
        "signal_kinds",
        "review_disposition",
        "post_text",
        "post_url",
        "author_name",
        "company_url",
        "last_seen_at",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(
            {
                "company": "Brad Smith",
                "author_name": "Brad Smith",
                "company_url": "https://www.linkedin.com/company/microsoft/posts/",
                "post_url": "https://www.linkedin.com/company/microsoft/posts/",
                "signal_kinds": "company_discovery;relevant_update",
                "review_disposition": "pending",
                "post_text": "Microsoft is building accessible AI products.",
            }
        )

    signals = company_signals_from_feed_ledger(path, run_id="run-1")
    skipped_known = company_signals_from_feed_ledger(
        path,
        run_id="run-1",
        known_companies=["Microsoft"],
    )

    assert [item.company_name for item in signals] == ["Microsoft"]
    assert skipped_known == []


def test_feed_company_signals_can_be_scoped_to_exact_capture_ids(tmp_path: Path) -> None:
    path = tmp_path / "feed.csv"
    fields = [
        "signal_id",
        "company",
        "signal_kinds",
        "review_disposition",
        "post_text",
        "post_url",
        "author_name",
        "first_seen_at",
        "last_seen_at",
        "observation_history_json",
        "company_url",
        "context",
        "relevance_reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for signal_id, company, observed_at in (
            ("current", "Current Labs", "2026-07-10T08:00:00+00:00"),
            ("old", "Old Labs", "2026-07-09T08:00:00+00:00"),
        ):
            writer.writerow(
                {
                    "signal_id": signal_id,
                    "company": company,
                    "signal_kinds": "company_discovery;hiring",
                    "review_disposition": "pending",
                    "post_text": "AI workflow company hiring product strategy",
                    "post_url": f"https://linkedin.test/{signal_id}",
                    "first_seen_at": observed_at,
                    "last_seen_at": observed_at,
                    "observation_history_json": json.dumps([observed_at]),
                }
            )

    by_id = company_signals_from_feed_ledger(
        path,
        run_id="run-current",
        signal_ids=["current"],
    )
    by_snapshot = company_signals_from_feed_ledger(
        path,
        run_id="run-current",
        observed_at="2026-07-10T08:00:00+00:00",
    )

    assert [item.company_name for item in by_id] == ["Current Labs"]
    assert [item.company_name for item in by_snapshot] == ["Current Labs"]
    assert by_id[0].provenance[0].source_run_id == "run-current"


def test_same_run_startup_report_becomes_independent_company_signals(tmp_path: Path) -> None:
    startup_report = tmp_path / "startup-source-report.json"
    startup_report.write_text(
        json.dumps(
            {
                "generated_at": "2026-07-10T08:00:00+00:00",
                "relationship_lane": {
                    "items": [
                        {
                            "verdict": "outreach_signal",
                            "source_id": "yc_sf_bay_hiring",
                            "organization_name": "Fresh Agent Co",
                            "company_url": "https://directory.test/fresh-agent-co",
                            "location": "San Francisco",
                            "batch": "S2026",
                            "team_size": "6 employees",
                            "tags": ["ai", "enterprise", "workflow"],
                            "description": "AI workflow agents for enterprise operators",
                            "reasons": [
                                "YC-backed/source-quality signal",
                                "active hiring/jobs signal",
                                "small-team founder/operator access",
                            ],
                        }
                    ]
                },
                "startup_apply": {"items": []},
            }
        ),
        encoding="utf-8",
    )
    metrics = tmp_path / "source-run-metrics.json"
    metrics.write_text(
        json.dumps({"startup_source_report": {"artifact": str(startup_report)}}),
        encoding="utf-8",
    )

    signals = company_signals_from_source_metrics(
        metrics,
        run_id="run-current",
        known_companies=["Already Known"],
    )

    assert [item.company_name for item in signals] == ["Fresh Agent Co"]
    assert signals[0].rubric.total >= 10
    assert signals[0].provenance[0].source_type == "startup_relationship"
    assert signals[0].provenance[0].source_run_id == "run-current"


def test_startup_relationship_signal_rejects_stale_source_artifact(tmp_path: Path) -> None:
    source_artifact = tmp_path / "discover-yc.json"
    source_artifact.write_text(json.dumps({"results": []}), encoding="utf-8")
    os.utime(source_artifact, (100, 100))
    startup_report = tmp_path / "startup-source-report.json"
    startup_report.write_text(
        json.dumps(
            {
                "relationship_lane": {
                    "artifacts": {
                        "yc_sf_bay_hiring": {
                            "artifact": str(source_artifact),
                            "status": "loaded",
                        }
                    },
                    "items": [
                        {
                            "source_id": "yc_sf_bay_hiring",
                            "organization_name": "Stale Co",
                            "description": "AI workflow startup",
                        }
                    ],
                },
                "startup_apply": {"items": []},
            }
        ),
        encoding="utf-8",
    )
    metrics = tmp_path / "source-run-metrics.json"
    metrics.write_text(
        json.dumps(
            {
                "run_started_at": "1970-01-01T00:03:20+00:00",
                "sources": {
                    "startup_relationship": {"status": "ran"},
                    "startup_apply": {"status": "skipped"},
                },
                "startup_source_report": {"artifact": str(startup_report)},
            }
        ),
        encoding="utf-8",
    )

    assert company_signals_from_source_metrics(metrics, run_id="current") == []


def test_role_inputs_use_only_artifacts_recorded_by_run(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps({"jobs": [{"company": "A", "title": "Business Operations Intern", "url": "https://job/1"}]}), encoding="utf-8")
    metrics = tmp_path / "metrics.json"
    metrics.write_text(json.dumps({"sources": {"jobspy": {"status": "ran", "details": {"raw_artifact": str(jobs)}}, "handshake": {"status": "skipped", "details": {}}}}), encoding="utf-8")

    observations, source_runs = role_inputs_from_source_metrics(metrics, run_id="run-1")

    assert len(observations) == 1
    assert observations[0].stage == RoleStage.DISCOVERED
    assert observations[0].title == "Business Operations Intern"
    assert {item.source: item.status for item in source_runs} == {"jobspy": SourceRunStatus.RAN, "handshake": SourceRunStatus.SKIPPED}


def test_timed_out_source_is_reported_as_failed(tmp_path: Path) -> None:
    metrics = tmp_path / "metrics.json"
    metrics.write_text(
        json.dumps({"sources": {"linkedin": {"status": "timed_out", "details": {}}}}),
        encoding="utf-8",
    )

    _, source_runs = role_inputs_from_source_metrics(metrics, run_id="run-1")

    assert source_runs[0].status == SourceRunStatus.FAILED

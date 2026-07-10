from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from outreach.role_surface_monitor import (
    CoverageStatus,
    RoleFamily,
    RoleObservation,
    RoleStage,
    SourceRun,
    SourceRunStatus,
    build_role_surface_report,
    classify_role_title,
    write_role_surface_artifacts,
)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senior Product Manager, AI", RoleFamily.PRODUCT_PM),
        ("Growth Product Manager", RoleFamily.PRODUCT_PM),
        ("Director of Product Strategy", RoleFamily.PRODUCT_STRATEGY),
        ("Business Operations Manager", RoleFamily.BIZOPS_STRATEGY),
        ("Chief of Staff to the CEO", RoleFamily.BIZOPS_STRATEGY),
        ("Technical Program Manager", RoleFamily.PROGRAM_OPERATIONS),
        ("Growth Strategy Lead", RoleFamily.GROWTH_ADJACENT),
        ("Growth Operations Manager", RoleFamily.GROWTH_ADJACENT),
        ("Head of Growth", RoleFamily.GROWTH_ADJACENT),
        ("Growth Marketing Manager", RoleFamily.OTHER),
        ("Revenue Operations Manager", RoleFamily.OTHER),
        ("Enterprise Account Executive", RoleFamily.OTHER),
    ],
)
def test_role_family_classifier_keeps_growth_lane_narrow(
    title: str,
    expected: RoleFamily,
) -> None:
    assert classify_role_title(title).family == expected


def test_role_surface_report_is_run_scoped_and_explicit_about_skipped_sources() -> None:
    run_id = "nightly-2026-07-10"
    observations = [
        RoleObservation(
            run_id=run_id,
            source="LinkedIn",
            title="Senior Product Manager",
            company="Acme",
            stage=RoleStage.SURFACED,
            source_url="https://linkedin.com/jobs/1",
        ),
        RoleObservation(
            run_id=run_id,
            source="JobSpy",
            title="Senior Product Manager",
            company="Acme",
            stage=RoleStage.ACTED,
            source_url="https://jobs.example/acme-pm",
        ),
        RoleObservation(
            run_id=run_id,
            source="LinkedIn",
            title="Business Operations Manager",
            company="Beta",
            stage=RoleStage.DISCOVERED,
        ),
        RoleObservation(
            run_id=run_id,
            source="JobSpy",
            title="Software Engineer",
            company="Gamma",
            stage=RoleStage.SCORED,
        ),
    ]
    source_runs = [
        SourceRun(run_id=run_id, source="LinkedIn", status=SourceRunStatus.RAN),
        SourceRun(run_id=run_id, source="JobSpy", status=SourceRunStatus.RAN),
        SourceRun(
            run_id=run_id,
            source="Handshake",
            status=SourceRunStatus.SKIPPED,
            reason="Disabled for this cycle",
        ),
    ]

    report = build_role_surface_report(
        run_id=run_id,
        observations=observations,
        source_runs=source_runs,
        generated_at="2026-07-10T10:00:00+00:00",
    )

    assert report.summary["observations"] == 4
    assert report.summary["unique_roles"] == 3
    assert report.summary["primary_product_roles"] == 1
    assert report.summary["adjacent_roles"] == 1
    assert report.summary["unclassified_roles"] == 1
    product = next(
        item for item in report.family_coverage if item.family == RoleFamily.PRODUCT_PM
    )
    bizops = next(
        item for item in report.family_coverage if item.family == RoleFamily.BIZOPS_STRATEGY
    )
    product_strategy = next(
        item for item in report.family_coverage if item.family == RoleFamily.PRODUCT_STRATEGY
    )
    handshake = next(item for item in report.source_coverage if item.source == "Handshake")

    assert product.discovered == 1
    assert product.surfaced == 1
    assert product.acted == 1
    assert product.coverage_status == CoverageStatus.MET
    assert bizops.discovered == 1
    assert bizops.coverage_status == CoverageStatus.MET
    assert product_strategy.discovered == 0
    assert product_strategy.coverage_status == CoverageStatus.MISSED
    assert handshake.status == SourceRunStatus.SKIPPED
    assert handshake.reason == "Disabled for this cycle"
    assert handshake.observations == 0
    assert handshake.discovered == 0
    handshake_matrix = [
        item for item in report.source_family_coverage if item.source == "Handshake"
    ]
    assert len(handshake_matrix) == 6
    assert all(item.discovered == 0 for item in handshake_matrix)
    assert [item.title for item in report.unclassified_roles] == ["Software Engineer"]
    assert "Product Strategy" in report.summary["families_below_floor"]


def test_role_surface_flags_unreported_sources_without_claiming_they_ran() -> None:
    report = build_role_surface_report(
        run_id="run-1",
        observations=[
            RoleObservation(
                run_id="run-1",
                source="Feed import",
                title="Product Operations Manager",
                company="Acme",
                stage=RoleStage.DISCOVERED,
            )
        ],
        source_runs=[],
    )

    assert report.source_coverage[0].status == SourceRunStatus.NOT_REPORTED
    assert report.source_coverage[0].discovered == 1
    assert report.summary["sources_ran"] == 0
    assert any("did not report" in warning for warning in report.warnings)
    assert all(
        item.coverage_status == CoverageStatus.NO_SOURCE_RAN
        for item in report.family_coverage
    )


def test_role_surface_rejects_mixed_run_inputs() -> None:
    with pytest.raises(ValueError, match="must be scoped to run"):
        build_role_surface_report(
            run_id="run-current",
            observations=[
                RoleObservation(
                    run_id="run-old",
                    source="JobSpy",
                    title="Product Manager",
                    company="Acme",
                )
            ],
            source_runs=[
                SourceRun(
                    run_id="run-current",
                    source="JobSpy",
                    status=SourceRunStatus.RAN,
                )
            ],
        )


def test_role_surface_writes_reusable_json_and_csv_outputs(tmp_path: Path) -> None:
    report = build_role_surface_report(
        run_id="run-2",
        observations=[
            RoleObservation(
                run_id="run-2",
                source="JobSpy",
                title="Product Strategy Manager",
                company="Acme",
                stage=RoleStage.SCORED,
            )
        ],
        source_runs=[
            SourceRun(run_id="run-2", source="JobSpy", status=SourceRunStatus.RAN),
            SourceRun(run_id="run-2", source="Handshake", status=SourceRunStatus.SKIPPED),
        ],
        generated_at="2026-07-10T11:00:00+00:00",
    )

    artifacts = write_role_surface_artifacts(tmp_path, report)
    payload = json.loads(artifacts.report_json.read_text(encoding="utf-8"))
    with artifacts.family_csv.open(newline="", encoding="utf-8") as handle:
        family_rows = list(csv.DictReader(handle))
    with artifacts.source_csv.open(newline="", encoding="utf-8") as handle:
        source_rows = list(csv.DictReader(handle))
    with artifacts.source_family_csv.open(newline="", encoding="utf-8") as handle:
        source_family_rows = list(csv.DictReader(handle))

    assert payload["run_id"] == "run-2"
    assert payload["summary_text"].startswith("Role surface:")
    assert len(family_rows) == 6
    assert {row["source"] for row in source_rows} == {"JobSpy", "Handshake"}
    assert len(source_family_rows) == 12
    assert artifacts.unclassified_csv.exists()

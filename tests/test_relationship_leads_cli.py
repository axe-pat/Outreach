from pathlib import Path

from typer.testing import CliRunner

from outreach.cli import app


runner = CliRunner()


def test_import_command_never_creates_a_missing_source_template(tmp_path: Path) -> None:
    source = tmp_path / "missing_relationship_leads.csv"
    workspace = tmp_path / "workspace"

    result = runner.invoke(
        app,
        [
            "import-relationship-leads",
            "--workspace",
            str(workspace),
            "--source-path",
            str(source),
            "--execute",
        ],
    )

    assert result.exit_code == 2
    assert "source file not found" in result.output
    assert not source.exists()
    assert not workspace.exists()

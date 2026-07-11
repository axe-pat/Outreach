from __future__ import annotations

from pathlib import Path

import pytest

from outreach.config import OutreachSettings


@pytest.fixture(autouse=True)
def isolate_outreach_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep command-level tests out of the production artifacts directory."""

    artifact_dir = tmp_path / "artifacts"
    monkeypatch.setenv("OUTREACH_ARTIFACTS_DIR", str(artifact_dir))
    # Some command modules construct settings through pydantic paths that tests
    # replace or cache. Pin the public property as well so every in-process
    # writer resolves to this test's isolated directory.
    monkeypatch.setattr(
        OutreachSettings,
        "artifacts_dir",
        property(lambda _settings: artifact_dir),
    )

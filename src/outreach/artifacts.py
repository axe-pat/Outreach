from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def write_artifact(base_dir: Path, name: str, payload: dict[str, Any]) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_dir.mkdir(parents=True, exist_ok=True)
    target = base_dir / f"{timestamp}-{name}.json"
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target


def artifact_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")

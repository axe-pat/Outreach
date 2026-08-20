#!/usr/bin/env python3
"""Report workbook organizations whose contact signals contradict identity."""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from outreach.reply_engine.org_identity import (  # noqa: E402
    audit_org_identities,
    render_identity_report,
)
from outreach.tracking import ContactRecord, OrganizationRecord  # noqa: E402


def _load(path: Path, model):
    with path.open(newline="", encoding="utf-8") as handle:
        rows = csv.DictReader(handle)
        fields = set(model.model_fields)
        return [
            model(**{key: value for key, value in row.items() if key in fields and value != ""})
            for row in rows
        ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default="workspace")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    workspace = REPO / args.workspace
    findings = audit_org_identities(
        _load(workspace / "organizations.csv", OrganizationRecord),
        _load(workspace / "contacts.csv", ContactRecord),
    )
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    output = Path(args.out) if args.out else REPO / "artifacts" / f"{stamp}-org-identity-audit.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_identity_report(findings), encoding="utf-8")
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.organization_id] = counts.get(finding.organization_id, 0) + 1
    print(f"likely_collision_orgs={sum(count >= 2 for count in counts.values())}")
    print(f"low_signal_orgs={sum(count == 1 for count in counts.values())}")
    print(f"findings={sum(counts.values())}")
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

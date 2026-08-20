#!/usr/bin/env python3
"""Surface parked LinkedIn contacts whose opportunity trigger has fired."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from outreach.reply_engine.reopen import check_reopen_conditions  # noqa: E402
from outreach.tracking import OutreachWorkbook  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default="workspace")
    parser.add_argument("--artifacts-dir", default="artifacts")
    parser.add_argument("--season", default="fall")
    args = parser.parse_args()

    output, assessments = check_reopen_conditions(
        workbook=OutreachWorkbook(REPO / args.workspace),
        artifacts_dir=REPO / args.artifacts_dir,
        pursuit_season=args.season,
    )
    candidates = sum(item.status == "reopen_candidate" for item in assessments)
    print(
        f"checked={len(assessments)} reopen_candidates={candidates} "
        f"artifact={output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

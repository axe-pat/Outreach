#!/usr/bin/env python3
"""Report workbook contacts whose normalized full names are not unique."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def normalize_name(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").casefold()))


def duplicate_groups(contacts_path: Path) -> list[dict[str, object]]:
    with contacts_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        key = normalize_name(row.get("full_name", ""))
        if key:
            grouped[key].append(row)

    results: list[dict[str, object]] = []
    for key, matches in grouped.items():
        if len(matches) < 2:
            continue
        results.append(
            {
                "normalized_name": key,
                "display_name": matches[0].get("full_name", ""),
                "count": len(matches),
                "contacts": [
                    {
                        "contact_id": row.get("contact_id", ""),
                        "organization_id": row.get("organization_id", ""),
                        "title": row.get("title", ""),
                        "linkedin_url": row.get("linkedin_url", ""),
                    }
                    for row in sorted(matches, key=lambda item: item.get("contact_id", ""))
                ],
            }
        )
    return sorted(results, key=lambda item: (-int(item["count"]), str(item["display_name"])))


def render_markdown(payload: dict[str, object]) -> str:
    groups = list(payload["duplicates"])
    lines = [
        "# Duplicate contact-name audit",
        "",
        f"Duplicate names: **{payload['duplicate_names']}** · affected rows: **{payload['affected_rows']}**",
        "",
        "A display name is not sufficient to bind these contacts. Use the LinkedIn profile URL.",
        "",
    ]
    for group in groups:
        lines.extend([f"## {group['display_name']} ({group['count']})", ""])
        for contact in group["contacts"]:
            lines.append(
                f"- `{contact['contact_id']}` · `{contact['organization_id']}` · "
                f"{contact['title'] or '(no title)'} · {contact['linkedin_url'] or '(no profile URL)'}"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contacts", type=Path, default=REPO / "workspace" / "contacts.csv")
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO / "artifacts" / "duplicate-contact-name-audit.json",
    )
    args = parser.parse_args()

    groups = duplicate_groups(args.contacts)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "contacts_source": str(args.contacts),
        "duplicate_names": len(groups),
        "affected_rows": sum(int(group["count"]) for group in groups),
        "duplicates": groups,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    markdown_path = args.output.with_suffix(".md")
    markdown_path.write_text(render_markdown(payload), encoding="utf-8")
    print(json.dumps({**payload, "duplicates": groups}, indent=2))
    print(f"markdown={markdown_path}")


if __name__ == "__main__":
    main()

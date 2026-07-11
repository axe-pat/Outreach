# peoplegrove_usc relationship lead capture

CSV: `relationship_leads_peoplegrove_usc.csv`

Required columns:

- `full_name`
- `company`

Recommended columns:

- `title`
- `linkedin_url`
- `company_website` or `company_linkedin_url`
- `school`, `program`, `grad_year`
- `relationship_signal`
- `notes`
- `source_url` or `source_record_id` (required provenance for PeopleGrove)
- `capture_batch`, `captured_at`, `captured_by`

Defaults applied during import when a row leaves fields blank:

- `source_type`: `peoplegrove`
- `target_lists`: `peoplegrove;usc-network`
- `tags`: `usc,peoplegrove,warm-network`
- `priority`: `medium`

Capture rules:

- Use this for PeopleGrove/Trojan Network/USC manual captures.
- Prioritize founders, C-suite, product leaders, operators, recruiters, and startup-adjacent USC people.
- Assign role-specific lists such as `usc-founder`, `usc-product`,
  `usc-startup-operator`, or `usc-recruiting` per curated row. Do not apply
  founder/operator lists to the whole source batch.
- Paste one person per row. company and full_name are required; LinkedIn URL is strongly preferred.

Review workflow:

1. Stage: `python main.py stage-relationship-leads --source-path workspace/relationship_leads_peoplegrove_usc.csv --source-key peoplegrove_usc`
2. Inspect the staged CSV and its validation issues.
3. Record explicit approvals/rejections with `review-relationship-leads`.
4. Import the reviewed staged CSV with `import-relationship-leads --execute`.

Raw capture CSVs cannot be executed directly.

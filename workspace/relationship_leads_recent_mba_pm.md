# recent_mba_pm relationship lead capture

CSV: `relationship_leads_recent_mba_pm.csv`

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
- `source_url` or `source_record_id`
- `capture_batch`, `captured_at`, `captured_by`

Defaults applied during import when a row leaves fields blank:

- `source_type`: `recent_mba_pm`
- `target_lists`: `recent-mba-pm;mba-network;product-transition`
- `tags`: `recent-mba-pm,mba,product`
- `priority`: `medium`

Capture rules:

- Use this for recent MBA grads who moved into PM/product/product strategy.
- Best rows include school/program/grad_year plus current company/title.
- This is a relationship lead lane, not an account-power lane; keep the ask mentorship/routing oriented.

Review workflow:

1. Stage: `python main.py stage-relationship-leads --source-path workspace/relationship_leads_recent_mba_pm.csv --source-key recent_mba_pm`
2. Inspect the staged CSV and its validation issues.
3. Record explicit approvals/rejections with `review-relationship-leads`.
4. Import the reviewed staged CSV with `import-relationship-leads --execute`.

Raw capture CSVs cannot be executed directly.

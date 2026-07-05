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

Defaults applied during import when a row leaves fields blank:

- `source_type`: `recent_mba_pm`
- `target_lists`: `recent-mba-pm;mba-network;product-transition`
- `tags`: `recent-mba-pm,mba,product`
- `priority`: `medium`

Capture rules:

- Use this for recent MBA grads who moved into PM/product/product strategy.
- Best rows include school/program/grad_year plus current company/title.
- This is a relationship lead lane, not an account-power lane; keep the ask mentorship/routing oriented.

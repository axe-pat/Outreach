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

Defaults applied during import when a row leaves fields blank:

- `source_type`: `peoplegrove`
- `target_lists`: `peoplegrove;usc-network;usc-founder;usc-operator`
- `tags`: `usc,peoplegrove,warm-network`
- `priority`: `medium`

Capture rules:

- Use this for PeopleGrove/Trojan Network/USC manual captures.
- Prioritize founders, C-suite, product leaders, operators, recruiters, and startup-adjacent USC people.
- Paste one person per row. company and full_name are required; LinkedIn URL is strongly preferred.

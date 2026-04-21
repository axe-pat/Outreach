# TODO

## Discovery Architecture

- Evaluate a shared discovery layer that ingests both ResumeGenerator job pulls and Outreach startup/company sources into one canonical store before downstream actioning.
- Design reverse cross-pollination from Outreach startup discovery back into application intake, ideally as a lightweight export/review step before any direct write into `jobs.xlsx`.
- Add a merged daily queue that combines:
  - apply-backed outreach from `ResumeGenerator v1/discovery/jobs.xlsx`
  - hiring startups from YC / Built In discovery
  - optional warm-startup outreach targets without live roles
- Decide whether the long-term canonical store should be one workbook with multiple sheets or a separate shared project/module.

## Outreach Messaging

- Add non-PM note-generation families so outreach copy matches the target role instead of defaulting to PM-oriented phrasing.
- Cover at least these role buckets:
  - Strategy / BizOps
  - Program / Operations
  - GTM / Revenue / Growth
  - General business fallback
- Remove hardcoded "pivoting into PM" / "exploring PM roles" language for candidates outside true product roles.
- Keep PM-specific language only for Product / Product Ops / Product-adjacent outreach.

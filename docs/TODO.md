# TODO

## Discovery Architecture

- Evaluate a shared discovery layer that ingests both ResumeGenerator job pulls and Outreach startup/company sources into one canonical store before downstream actioning.
- Design reverse cross-pollination from Outreach startup discovery back into application intake, ideally as a lightweight export/review step before any direct write into `jobs.xlsx`.
- Add a high-affinity LinkedIn expansion pass for `application_plus_outreach` companies before sends:
  - exact-company people search remains the base pass
  - add targeted passes for shared-history keywords such as `Intuit`, `Gojek`, `USC`, `Marshall`, plus `Product`, `hiring`, and product leadership terms
  - raise daily send caps for companies with strong job fit plus strong affinity signals
  - optionally inspect full profiles only for top-priority companies where the card result misses obvious commonalities
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

## Email Channel

- Add and validate an email-finder provider key before enabling daily email research:
  - preferred: `PROSPEO_API_KEY`
  - fallback: `HUNTER_API_KEY`
  - keep `--max-email-research 0` in the daily live runner until one provider is configured and a small paid/credit-bounded test passes.

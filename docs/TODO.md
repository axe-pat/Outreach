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

- LinkedIn Contact Info email research is enabled inside Track 2 with a current cap of 10/day.
- Add and validate an external email-finder provider key before enabling provider-backed fallback lookups:
  - preferred: `PROSPEO_API_KEY`
  - fallback: `HUNTER_API_KEY`
  - keep external provider lookup opt-in until one provider is configured and a small paid/credit-bounded test passes.

## Track 2 Daily Runner

- Daily report wiring is now on the active ResumeGenerator LaunchAgent path:
  - the nightly pipeline calls Outreach `write-daily-run-report` after live discovery, sends, maintenance, and campaign planning.
  - current nightly cycle is `offcycle_light`: app-queue LinkedIn invites cap at 5, while Track 2 owns relationship sends with caps of 25 new invites, 25 follow-ups, 15 mapping tasks, and 10 LinkedIn Contact Info/email research tasks.
  - switch back to `--cycle-config normal` when app volume returns so the app-queue invite cap moves back to 25.
  - `scripts/run_manual_supervised_e2e_debug.sh` is the attended/manual Outreach debug runner; `scripts/run_daily_supervised_e2e.sh` is only a warning shim for backwards compatibility.
  - latest daily HTML lives in `workspace/reports/daily_html/`, with compatibility mirrors in `workspace/reports/` and `workspace/`.
  - the stale standalone Outreach crontab entry should stay removed so there is one blessed scheduled runner.
- Tune live daily breadth after 2-3 high-volume Track 2 runs:
  - watch LinkedIn restriction signals, send failures, accept/reply rate, and report-level company progress before increasing above 25 Track 2 invites/day.
  - mapping cap is 15 because a 25-invite outflow usually drains relevant pools across 8-12 companies and some mapping passes produce no safe candidate.
  - keep ResumeGenerator discovery on its normal daily budget; if it hangs again, fix per-source timeouts inside ResumeGenerator rather than shrinking the whole discovery lane.
- Keep the HTML report as the review surface:
  - include last inbound LinkedIn message for each review item.
  - show review/hold drafts without requiring the raw JSON payload.
  - keep company-level actions grouped by tier/phase.

## Relationship Sources

- Populate `workspace/relationship_leads.csv` from manual PeopleGrove/USC/recent-MBA-PM pulls when ready.
- Keep these as one-time or low-frequency source imports, not daily scrapers.

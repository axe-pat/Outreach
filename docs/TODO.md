# TODO

## Operating Priorities: Next 4–6 Weeks

The objective is not maximum messages or a perfect database. It is to create
enough high-quality conversations and live application paths to land a product
role, while keeping genuinely strong Product Strategy, BizOps, and Operations
paths visible instead of accidentally filtering them out.

1. **Make the daily loop trustworthy and inspectable.** Run the nightly pipeline
   for several real cycles, then use its run-scoped report to correct source
   failures, review backlog, LinkedIn restrictions, and campaign execution.
   This comes first: every later optimization is noise if it is based on stale
   snapshots or missing source runs.
2. **Build a company-discovery and promotion loop.** Do not depend on a hand-
   built company list. Pull promising companies from the daily LinkedIn home
   feed, startup/company sources, hiring/funding/news signals, and warm-network
   activity; normalize them into a small candidate queue; then apply the target
   rubric—domain fit, technical-MBA story, geography/remote viability,
   growth/quality, and plausible Product or adjacent-role surface—to promote
   only the best ones into the company-level watchlist. The existing tracker is
   useful memory after discovery, not a substitute for discovery.
3. **Close the channel loop: cold email plus follow-up.** Start with a small,
   human-reviewed batch for high-fit accounts that have a verified email or a
   meaningful warm path. Add a channel-aware cadence and stop rules before
   increasing volume; Track 2 must record email and LinkedIn touches together.
4. **Protect role coverage, without diluting the product thesis.** Audit the
   ResumeGenerator role classifiers, queries, and queue outputs by role family:
   Product/PM, Product Strategy, BizOps/Strategy, Program/Operations, and a
   narrowly defined Growth/GTM-adjacent lane. Set a weekly coverage floor and
   report candidates found, scored, surfaced, and acted on for each family.
5. **Add high-signal LinkedIn discovery, not more undifferentiated scraping.**
   Run a lightweight daily home-feed capture (about a minute): save only actionable job,
   hiring, funding, launch, and warm-network signals with the post URL and
   source context. Treat the feed as a discovery source that can create an
   opportunity, account signal, or contact task—not as an automatic messaging
   queue.
6. **Use relationship signals deliberately.** Add a weekly profile-viewer
   review, dedupe it against existing contacts, and promote only relevant
   viewers to a contextual follow-up/research queue. A profile view alone is a
   soft signal; it should never trigger an automatic cold message.
7. **Improve selection from feedback.** Use the gold/silver/negative comms
   corpus, replies, accepts, and application outcomes to rebalance account
   priority and message patterns. Expand throughput only after the first five
   priorities are producing clean, reviewed evidence.

### Cadence and Stop Rules to Implement

- **Initial LinkedIn → follow-up:** after an invite is accepted but has no reply,
  send one useful follow-up around day 4. Do not send a generic nudge to a
  pending invite.
- **Second follow-up:** at most one, around day 10–14 after that, only with a distinct
  value-add (role, launch, referral context, thoughtful question, or channel
  switch). Then pause for 60–90 days unless the person re-engages.
- **Cold email:** one targeted email to a verified address; one follow-up after
  7–10 days; optional final close-the-loop follow-up 14–21 days later. No more
  than three total email touches in a 90-day window without engagement.
- **Cross-channel:** do not send LinkedIn and email on the same day unless a
  warm event makes it natural. The tracker must show all touches before drafting
  the next one.
- **Profile viewers:** review weekly, score by account fit/shared context, and
  research before writing; no automatic sends.

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
- Build a company-discovery and promotion loop: ingest candidate companies from
  LinkedIn home-feed signals, startup/company sources, hiring/funding/news, and
  warm-network activity; score candidates against the target rubric; and create
  a human-reviewed company watchlist. The goal is to discover strong companies
  before a role or search query happens to put them in front of us.
- Add a lightweight daily LinkedIn home-feed ingestion pass that records
  actionable posts with source URL, author/company, signal type, and a review
  decision. It must be a provenance-preserving discovery source, not a bulk
  scraper or auto-send trigger.
- Add a weekly LinkedIn profile-viewer review with dedupe, account-fit scoring,
  and a contextual research/review queue.

## Outreach Messaging

- Add non-PM note-generation families so outreach copy matches the target role instead of defaulting to PM-oriented phrasing.
- Cover at least these role buckets:
  - Strategy / BizOps
  - Program / Operations
  - GTM / Revenue / Growth
  - General business fallback
- Remove hardcoded "pivoting into PM" / "exploring PM roles" language for candidates outside true product roles.
- Keep PM-specific language only for Product / Product Ops / Product-adjacent outreach.
- Implement explicit LinkedIn/email cadence enforcement and stop rules across
  touchpoints; include cross-channel suppression and a 60–90-day cool-down.
- Add a small, reviewed cold-email lane for verified addresses, including one
  follow-up and an optional final close-the-loop note.

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
  - scheduled reports are run-scoped: Source Breakdown is anchored to the nightly summary and explicitly marks LinkedIn, Handshake, JobSpy, startup sources, ResumeGenerator/app queue, and Track 2 as ran/skipped. Do not present a workspace snapshot or a newest artifact as evidence for a prior run.
  - comms-learning artifacts are persisted under `workspace/comms_learning/`: manual sends are gold, replaced/cleared generated drafts are negative, and approved/automatic drafts actually sent are silver. Use the corpus to improve future messaging rather than merely deleting stale review rows.
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

## Role-Family Coverage

- Keep the account tracker company-level. Add a separate role-surface monitor
  that reports whether the discovery/application lanes are surfacing Product
  Strategy, BizOps/Strategy, Program/Operations, and narrowly defined
  Growth/GTM-adjacent roles alongside the primary Product lane.
- Audit ResumeGenerator title/query filters and scoring so those families are
  discovered, scored, and surfaced rather than silently treated as generic
  non-PM roles or deprioritized sales.
- Add account-level role-watch tasks for strategic companies: a company can
  remain active even when it has no current PM role, while a good adjacent
  opening should create an application/research action.

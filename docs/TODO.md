# TODO

## Operating Priorities: Items 2–7 Implemented, Live Validation Next

The objective is not maximum messages or a perfect database. It is to create
enough high-quality conversations and live application paths to land a product
role, while keeping genuinely strong Product Strategy, BizOps, and Operations
paths visible instead of accidentally filtering them out.

1. **ACTIVE — make the daily loop trustworthy and inspectable.** Run the
   nightly pipeline for several real cycles, then use its run-scoped report to
   correct source failures, review backlog, LinkedIn restrictions, and campaign
   execution. This comes first: every later optimization is noise if it is
   based on stale snapshots or missing source runs. The production contract is
   fail-closed: run claims come only from the nightly summary's exact
   `daily_engine_manifest` and Track 2 phase pointers, never artifact mtimes.
   Missing manifests/artifacts, a `None` Track 2 return code, queued live phases,
   or phase failures must remain visibly incomplete/not-run and can never be
   rendered as completed. `What needs you`, message review, auto-handled sends,
   system holds, actual company actions, and plans remain separate report data.
2. **IMPLEMENTED — operate the daily LinkedIn intelligence pass.** The
   configurable home-feed capture records good startups and other interesting
   company, role, hiring, funding, launch, and warm-network signals with post
   and author provenance. It never auto-messages and has no hard-coded 60-second
   limit. The current selectors were live-validated on July 10, 2026; the next
   work is tuning `max_scrolls`, `max_items`, and the optional duration cap from
   several scheduled runs.
3. **IMPLEMENTED — review and promote independently discovered companies.**
   Feed signals can become candidate companies without already existing in
   `organizations.csv`. Dedupe, provenance, the five-part fit rubric, editable
   review state, and JSON/CSV watchlist outputs are in place. Promotion into the
   company tracker requires both rubric qualification and explicit human
   approval. Same-run YC/Built In startup discovery is also connected; next add
   further company/news directories through the same input contract.
4. **IMPLEMENTED, LIVE GATE CLOSED — cold email and cross-channel cadence.**
   Tracker-backed guards now cover LinkedIn and email timing, maximum touches,
   replies/stops, same-day channel suppression, and distinct second LinkedIn
   follow-ups. SMTP delivery exists only behind reviewed drafts, a due cadence
   decision, configured credentials, a bounded limit, and explicit `--execute`.
   The report keeps email drafts in `Messages to review`, surfaces SMTP/config
   blockers in `What needs you` and Source Breakdown, and counts only exact
   delivery results as sends. The next step is one small reviewed live batch,
   not higher volume.
5. **IMPLEMENTED MONITOR; TUNING REMAINS — protect adjacent role coverage.**
   A separate run-scoped role-surface report keeps the account tracker
   company-level while showing discovered, scored, surfaced, and acted-on
   Product/PM, Product Strategy, BizOps/Strategy, Program/Operations, and narrow
   Growth-adjacent roles by source. Product remains primary. Use real reports to
   audit and then tune ResumeGenerator queries/classifiers where adjacent lanes
   are genuinely missing; the monitor itself does not prove discovery breadth
   is already adequate.
6. **IMPLEMENTED — retain profile viewers as weekly passive context.** The
   LinkedIn intelligence pass checks the viewer ledger every seven days by
   default, dedupes repeated observations, and annotates target-company/role
   relevance. It never creates an automatic outreach trigger. The current live
   page was validated on July 10, 2026; monitor it for DOM/access changes.
7. **IMPLEMENTED — learn from messages and outcomes.** The
   gold/silver/negative corpus is combined with tracker accepts, replies,
   rejections, message types, audiences, and accounts. Labeled messages sync
   into the reusable style profile as bounded strong/weak examples. Aggregate
   recommendations remain human-reviewed and do not automatically rewrite
   prompt rules, account rubrics, or selection policy. Accumulate enough real
   outcomes before making those higher-order changes.

### Remaining Activation and Human Gates

- [x] Add report contract tests proving a concurrent/manual/pytest artifact in
  the same directory and time window cannot contaminate production run totals.
- [ ] Require a final daily-engine manifest and exact artifact reconciliation
  in the scheduled production run before treating its report as green.
- [ ] Keep new pipeline stages disabled in production until isolated tests and
  a fixture-backed end-to-end report test pass on a feature branch; merge and
  enable them only after those gates succeed.

- [x] Validate the signed-in dedicated Chrome session against the current
  LinkedIn feed and profile-viewer pages (live-validated July 10, 2026).
- [ ] Observe the capture for several scheduled daily cycles before changing
  its budget.
- [ ] Review useful feed signals and set dispositions; review the generated
  company CSV and explicitly approve candidates before running
  `build-company-discovery-review --promote-approved`.
- [ ] Pass the current run's ResumeGenerator source-metrics JSON into the role
  monitor. Never fill a missing source with an older workspace artifact.
- [ ] Configure `SMTP_HOST` and `SMTP_FROM_EMAIL` plus the appropriate port,
  username/password, STARTTLS/SSL settings; verify sender authorization outside
  the production batch.
- [ ] Confirm each live email has a verified address, an approved draft, and a
  tracker-backed due action; preview first, then run a bounded `--execute`
  batch. SMTP is not enabled silently by the nightly pipeline.
- [ ] Configure `PROSPEO_API_KEY` or `HUNTER_API_KEY` only if external email
  lookup is intentionally enabled; LinkedIn Contact Info research can remain
  the primary bounded path.
- [ ] Review outcome-learning recommendations only after sufficient sample
  size. Any prompt, rubric, or selection-policy change remains a human decision.

### Implemented Cadence and Stop Rules

- **Initial LinkedIn → follow-up:** after an invite is accepted but has no reply,
  send one useful follow-up around day 4. Do not send a generic nudge to a
  pending invite.
- **Second follow-up:** at most one, around day 4–5 after that, only with a distinct
  value-add (role, launch, referral context, thoughtful question, or channel
  switch). The engine then marks the cadence complete; operationally wait
  60–90 days and require a real new hook or engagement before reconsidering.
- **Cold email:** one targeted email to a verified address; one follow-up after
  4 days; optional final close-the-loop follow-up 4–5 days later. No more than
  three total email touches in a 90-day window without engagement.
- **Cross-channel:** do not send LinkedIn and email on the same day unless a
  warm event makes it natural. The tracker must show all touches before drafting
  the next one.
- **Profile viewers:** capture/review weekly as passive interest context, score
  by account fit/shared context, and research before writing; no automatic
  sends.

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
- [x] Build the initial company-discovery and promotion loop: ingest candidates from
  LinkedIn home-feed signals, startup/company sources, hiring/funding/news, and
  warm-network activity; score candidates against the target rubric; and create
  a human-reviewed company watchlist. The goal is to discover strong companies
  before a role or search query happens to put them in front of us. Current
  inputs are the feed ledger plus same-run YC/Built In startup source artifacts;
  add further source adapters through the same candidate contract rather than
  bypassing review.
- [x] Add a lightweight daily LinkedIn home-feed ingestion pass that records
  actionable posts with source URL, author/company, signal type, and a review
  decision. It must be a provenance-preserving discovery source, not a bulk
  scraper or auto-send trigger.
- [x] Add a weekly passive LinkedIn profile-viewer ledger with dedupe and
  account/role relevance context. Keep it informational; decide manually when a
  viewer warrants research rather than creating an automatic outreach action.

## Outreach Messaging

- [x] Add first-class target-role inference so LinkedIn invites, accepted/reply
  follow-ups, review suggestions, and Track 2 email sequences match the pursued
  role rather than the recipient's title. The selected daily-plan role is carried
  into both invite and email execution, while invite touchpoints preserve the
  family for later reconciliation.
- [x] Cover these role buckets while keeping Product primary within mixed
  opportunity sets:
  - Product / PM
  - Product Strategy
  - Strategy / BizOps
  - Program / Operations
  - narrow GTM / Growth strategy and operations
  - General business fallback
- [x] Remove hardcoded "pivoting into PM" / "exploring PM roles" language for
  concrete non-Product targets, including existing-connection, USC/Marshall,
  shared-history, follow-up/reply, review-suggestion, and email paths. Preserve
  factual references to a recipient's Product background.
- [x] Keep PM-specific target language only for Product / Product Ops outreach;
  do not add role rows or role fields to contacts in the company-level tracker.
- [x] Implement explicit LinkedIn/email cadence enforcement and stop rules
  across touchpoints, including cross-channel suppression, terminal/engagement
  stops, and bounded touches. After the final LinkedIn value-add, the engine
  stays complete until a real new hook or manual re-engagement decision rather
  than automatically creating another nudge.
- [x] Add a small, reviewed cold-email lane for verified addresses, including
  one follow-up and an optional final close-the-loop note. Keep the live SMTP
  gate closed until credentials and the first approved bounded batch are ready.

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
  - keep the report action-first: show actual per-company execution separately from planned campaigns, and surface open inbound LinkedIn actions (for example, resume requests) in `What needs you`. The persistent action queue is `workspace/linkedin_inbox_actions.csv`; it requires a human status update and never auto-sends an email.
  - comms-learning artifacts are persisted under `workspace/comms_learning/`: manual sends are gold, replaced/cleared generated drafts are negative, and approved/automatic drafts actually sent are silver. Use the corpus to improve future messaging rather than merely deleting stale review rows.
  - role-surface counts are run-scoped only when built from that run's source
    metrics. Company review/watchlist, cadence, profile-viewer, and outcome-
    learning counts are cumulative workspace-state snapshots even when they
    were refreshed during the current run. Label them accordingly; persistent
    rows are not current-run actions or proof a source ran.
  - ad-hoc report mode without `--since` plus `--nightly-summary` remains a
    clearly labeled workspace snapshot; only the paired-argument scheduled mode
    is authoritative for one nightly run.
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

- [x] Keep the account tracker company-level and add a separate role-surface monitor
  that reports whether the discovery/application lanes are surfacing Product
  Strategy, BizOps/Strategy, Program/Operations, and narrowly defined
  Growth/GTM-adjacent roles alongside the primary Product lane.
- Audit ResumeGenerator title/query filters and scoring so those families are
  discovered, scored, and surfaced rather than silently treated as generic
  non-PM roles or deprioritized sales.
- Add account-level role-watch tasks for strategic companies: a company can
  remain active even when it has no current PM role, while a good adjacent
  opening should create an application/research action.

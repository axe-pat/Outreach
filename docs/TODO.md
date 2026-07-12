# TODO

## Operating Priorities: Core Architecture Implemented, Live Validation Next

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
   rendered as completed. Required Source Breakdown rows now fail overall run
   health on failed/timeout/partial/incomplete statuses while intentional skips
   and successful zero-result runs with an exact artifact remain valid. A
   ran source with no raw count is incomplete, not a synthetic zero. `What
   needs you`, exact-run message review, carryover review backlog, auto-handled
   sends, system holds, actual company actions, and plans remain separate report
   data. A final cadence hold wins over an older review copy for the same person.
   Exact-company-filter failures, app-queue prep/send failures, and unknown
   invite delivery must likewise stay non-green and visible at company level;
   an unknown reserved invite requires signed-in reconciliation before retry.
2. **IMPLEMENTED — operate the daily LinkedIn intelligence pass.** The
   configurable home-feed capture records good startups and other interesting
   company, role, hiring, funding, launch, and warm-network signals with post
   and author provenance. It never auto-messages and has no hard-coded 60-second
   limit. The July 11 production proof exposed nested-card actor mismatches and
   six captures with zero post URLs. The extractor now binds fields to one
   update/actor block and requires at least one post plus a stable permalink for
   every retained post; empty or URL-less captures fail closed. The next 1am
   run is the live revalidation before tuning `max_scrolls`, `max_items`, or the
   optional duration cap.
3. **IMPLEMENTED — review and promote independently discovered companies.**
   Feed signals can become candidate companies without already existing in
   `organizations.csv`. Dedupe, provenance, the five-part fit rubric, editable
   review state, and JSON/CSV watchlist outputs are in place. Public startup/news
   RSS feeds and reviewed CSV/JSON imports now use the same candidate contract.
   Promotion into the
   company tracker requires both rubric qualification and explicit human
   approval. Same-run YC/Built In startup discovery is also connected; tune the
   broader feeds from real review yield rather than bypassing the gate.
4. **IMPLEMENTED, LIVE GATE CLOSED — cold email and cross-channel cadence.**
   Tracker-backed guards now cover LinkedIn and email timing, maximum touches,
   replies/stops, same-day channel suppression, and distinct second LinkedIn
   follow-ups. SMTP delivery exists only behind reviewed drafts, a due cadence
   decision, configured credentials, a bounded limit, and explicit `--execute`.
   The report keeps email drafts in `Messages to review`, surfaces SMTP/config
   blockers in `What needs you` and Source Breakdown, and counts only exact
   delivery results as sends. The next step is one small reviewed live batch,
   not higher volume.
5. **IMPLEMENTED AND EXACT-RUN VALIDATED; TUNING REMAINS — protect adjacent role coverage.**
   A separate run-scoped role-surface report keeps the account tracker
   company-level while showing discovered, scored, surfaced, and acted-on
   Product/PM, Product Strategy, BizOps/Strategy, Program/Operations, and narrow
   Growth-adjacent roles by source. Product remains primary. The final July 11
   replay uses the completed nightly run's exact source-metrics pointer and covers
   402 observations, 379 unique roles, and 220 companies from six ran sources,
   with no source failure/skip: Product 94 discovered and 5 scored/surfaced;
   Product Strategy 1; BizOps 8 and 2/2; Program/Ops 24 and 5/5; narrow Growth 7
   and 2/2. No configured family is below its floor
   (`artifacts/20260711-141856-role-surface-report.json`). Continue tuning and
   verify several scheduled runs before treating this as ongoing coverage.
   The app-queue bridge now preserves each selected target's exact role title,
   source, and queue bucket and passes `--target-role-title` into Outreach; this
   closes the AMETEK proof-run leak where company-only execution fell back to
   Product wording for an AI Automation role.
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
- [x] Add a fixture-backed report gate proving a timed-out required source stays
  visible in Source Breakdown and cannot render the overall run completed.
- [ ] Require a final daily-engine manifest and exact artifact reconciliation
  in the scheduled production run before treating its report as green.
- [ ] Keep new pipeline stages disabled in production until isolated tests and
  a fixture-backed end-to-end report test pass on a feature branch; merge and
  enable them only after those gates succeed.

- [x] Validate the signed-in dedicated Chrome session against the current
  LinkedIn feed and profile-viewer pages (live-validated July 10, 2026).
- [ ] Observe the capture for several scheduled daily cycles before changing
  its budget.
- [x] Review the initial feed/news queue and promote only explicit approvals.
  The July 11 queue has 11 unique companies: EdVisorly, Ollama, and PrimeIntellect
  are approved/promoted; Litmus, ZML, SambaNova, Apollo.io, and Oratomic remain
  needs-research; Hyperagent, USC BSEL, and a false Y Combinator attribution are
  rejected. Re-running promotion changes no organization rows and retains the
  durable three-company approved watchlist.
- [x] Validate the role monitor against the completed nightly run's exact
  ResumeGenerator source-metrics artifact. Never fill a missing source with an
  older workspace artifact.
- [ ] Confirm the same exact-pointer contract in several scheduled nightly runs;
  the standalone replay is not evidence that the production stage ran.
- [ ] Configure `SMTP_HOST` and `SMTP_FROM_EMAIL` plus the appropriate port,
  username/password, STARTTLS/SSL settings; verify sender authorization outside
  the production batch.
- [ ] Confirm each live email has a verified address, an approved draft, and a
  tracker-backed due action; preview first, then run a bounded `--execute`
  batch. SMTP is not enabled silently by the nightly pipeline.
- [ ] Configure `PROSPEO_API_KEY` or `HUNTER_API_KEY` only if external email
  lookup is intentionally enabled; LinkedIn Contact Info research can remain
  the primary bounded path.
- [x] Review the first outcome-learning recommendations after 623 sends, 49
  accepts, and 17 replies. The July 11 review preserves the aggregate LinkedIn
  follow-up pattern, keeps SLAC account-specific, and holds the three tiny account
  samples for more evidence. It applies no automatic prompt, rubric, or selection
  change (`workspace/comms_learning/outcome_recommendation_review_2026-07-11.json`).

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
  sends. Weekly cadence uses a durable last-attempt/last-success marker, so a
  successful zero-viewer capture waits seven days while a failed attempt retries
  on the next run.

## Discovery Architecture

- [x] Build a shared discovery layer that consumes one exact ResumeGenerator
  nightly/action-queue artifact plus Outreach company, watchlist, and warm-contact
  evidence. `src/outreach/shared_discovery.py` normalizes and dedupes by company,
  preserves roles/provenance/review state, and writes a run-stamped JSON/CSV queue
  whose embedded scope distinguishes the exact ResumeGenerator run from the
  Outreach/watchlist snapshots merged alongside it.
- [x] Keep reverse cross-pollination review-first: Outreach-only companies surface
  as company/research actions in the shared queue and never write directly into
  `jobs.xlsx`. A live role remains required before the application lane owns it.
- [x] Implement and live-canary validate the default-off high-affinity
  LinkedIn expansion pass for
  `application_plus_outreach` and other top, role-backed companies before sends:
  - exact-company people search remains the base pass
  - add targeted passes for shared-history keywords such as `Intuit`, `Gojek`,
    `USC`, `Marshall`, `Thapar`, `Hevo`, and `Optum`, plus role-family-specific
    product, hiring, leadership, strategy, operations, and narrow growth terms
  - raise per-company caps from 3 to at most 5 only when actual scored affinity
    candidates exist and unused global daily headroom remains
  - optionally inspect full profiles only for top-priority companies where the card result misses obvious commonalities
  - hard per-candidate worker timeouts, pre-attempt slot reservation, explicit
    `send_unknown_reserved` reconciliation, partial-progress preservation, and
    no-oversend tests now guard live execution
  - every live-send surface now fails closed when the exact company filter
    fails, and coverage-only/startup candidates require independent structured
    current-employer evidence; candidate names, cached match flags, and search
    pass labels never authorize a send
  - a second parallel July 11 nightly exposed the old gap by sending one
    off-company Julia invite. The run was stopped before its remaining sends,
    the invite was withdrawn in the signed-in session, no message was sent, and
    the contact is retained as `Do not contact` with the withdrawal audit
    (`artifacts/20260711-144345-invite-withdrawal-reconciliation.json`)
  - the July 11 Delinea canary found four raw affinity hits but only two qualified
    for lift, so the recommendation correctly stayed at the base cap of three
    (`artifacts/20260711-142731-dry-run-pipeline.json`)
  - keep `--enable-affinity-expansion` explicit/default-off until another bounded
    scheduled run confirms the production browser path under these guards
- [x] Add a merged daily queue that combines:
  - apply-backed outreach from the exact ResumeGenerator current-run action
    queue, ultimately derived from `ResumeGenerator v1/discovery/jobs.xlsx`
  - hiring startups from YC / Built In discovery
  - optional warm-startup outreach targets without live roles
- [x] Use a separate shared module/artifact rather than another workbook. The
  application tracker and Outreach entity workbook remain their lane-specific
  systems of record; the merged queue is a run-stamped decision surface with
  explicit per-input scope, not a third mutable tracker.
- [x] Build the initial company-discovery and promotion loop: ingest candidates from
  LinkedIn home-feed signals, startup/company sources, hiring/funding/news, and
  warm-network activity; score candidates against the target rubric; and create
  a human-reviewed company watchlist. The goal is to discover strong companies
  before a role or search query happens to put them in front of us. Current
  inputs are the feed ledger plus same-run YC/Built In startup source artifacts;
  public TechCrunch Startups and Crunchbase News RSS adapters plus repeatable
  reviewed CSV/JSON/JSONL inputs now use the same candidate contract rather than
  bypassing review. Hacker News remains opt-in because it is noisier.
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
  - scheduled reports are run-scoped: Source Breakdown is anchored to the nightly summary and explicitly marks LinkedIn job discovery, LinkedIn home feed, Handshake, JobSpy, startup sources, ResumeGenerator/app queue, and Track 2 as ran/skipped/failed. Do not present a workspace snapshot or a newest artifact as evidence for a prior run.
  - keep the report action-first: show actual per-company execution separately from planned campaigns, and surface open inbound LinkedIn actions (for example, resume requests) in `What needs you`. The persistent action queue is `workspace/linkedin_inbox_actions.csv`; it requires a human status update and never auto-sends an email.
  - the inbox refresh is independent of campaign-plan selection: zero planned
    follow-up companies must still scan for inbound replies within the 25-message
    cap. Unmatched threads become explicit mapping/review actions, and a clean
    zero-draft scan is recorded as completed-zero-actions.
  - contact mapping persists exact contacts and uses a bounded cross-functional
    pass set; each attempted company is shown with its own completed/failed and
    profile/contact counts, including zero-result attempts. Do not run every
    affinity/alumni expansion pass for all 15 nightly mapping companies.
  - comms-learning artifacts are persisted under `workspace/comms_learning/`: manual sends are gold, replaced/cleared generated drafts are negative, and approved/automatic drafts actually sent are silver. Use the corpus to improve future messaging rather than merely deleting stale review rows.
  - `Messages to review (this run)` and `Carryover review backlog (workspace
    snapshot)` are separate report contracts. Only the first can count toward
    exact-run outcome/review totals; final cadence holds suppress stale review
    copies for the same contact. The refreshed durable inbox state must also
    suppress carryover rows after a manual outbound reply and mark the matching
    persistent inbox action `manual_handled`.
  - keep `What needs you` human-only. Aggregate browser/CDP and channel
    configuration failures under `System issues`; do not turn one shared runtime
    failure into a separate company-identity task for every skipped company.
  - founder/C-suite and priority/strategic/story-fit initial invites and message
    drafts belong in the separate executive/high-value review queue. Recheck
    both fail-closed gates at the actual send boundary, including for older
    drafts labelled `safe_to_review` and already-selected invite batches.
  - keep sent, prepared, and draft counts explicit: invite candidates and
    persisted mapping touchpoints are prepared work, never outbound sends.
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

- [x] Complete the safe low-frequency relationship import path and its initial
  proof-of-flow seed. The July 4 PeopleGrove/Trojan Network work placed 28 USC
  profiles in the tracker; that is not complete source coverage. On July 11, an
  additional official-public-source batch added 11 reviewed leads: 6 USC
  founders/product leaders/operators and 5 recent MBA-to-product profiles, with
  source URLs and no guessed emails.
- [x] Require `validate → stage → explicit review → execute` for every later batch.
  Staged batch/row IDs, SHA-256 manifests, provenance, dedupe, tamper detection,
  finalized-decision locking, complete SHA-bound decision artifacts, non-mutating
  imports, and ambiguous workbook-identity conflict checks are implemented.
- [x] Complete the signed-in PeopleGrove/Trojan Network curation, review, and
  import. The July 11 pull captured 1,845 unique profiles across 12 targeted USC
  role/education queries; seven exact-count surfaces were exhausted and five
  high-volume surfaces remain honestly bounded best-match samples. Structural
  curation retained 154 and rejected 1,691 (`940` unparseable current
  role/company, `353` students/interns, `348` without a high-signal role, plus
  `50` other stale, irrelevant, duplicate, or job-seeker rows). Manual review
  approved 104 of the 154. A separate public-corroboration pass researched 111
  omitted/ambiguous profiles, resolved 51, and approved 31 more. The final 135
  reviewed people were imported with no guessed email or LinkedIn field; 1,710
  captured profiles were not imported. Exact reruns added or updated zero
  organizations and zero contacts, leaving 174 total relationship-source
  contacts including the earlier USC/MBA seeds. Keep this low-frequency and
  high-signal; do not turn it into a daily scraper.

## Role-Family Coverage

- [x] Keep the account tracker company-level and add a separate role-surface monitor
  that reports whether the discovery/application lanes are surfacing Product
  Strategy, BizOps/Strategy, Program/Operations, and narrowly defined
  Growth/GTM-adjacent roles alongside the primary Product lane.
- [x] Audit ResumeGenerator title/query filters and scoring so those families are
  discovered, scored, and surfaced rather than silently treated as generic
  non-PM roles or deprioritized sales. Product Strategy and narrow Growth query
  coverage are explicit; generic sales/marketing growth remains excluded.
- [x] Add account-level role-watch tasks for strategic companies through the
  existing shared queue, without creating another tracker. Strategic company rows
  remain company-level and surface as buffered `role_watch` items when no PM role
  exists. Strong exact-run Product Strategy, BizOps/Strategy,
  Program/Operations, or narrow Growth-adjacent openings upgrade the same item to
  human-gated `application_research` with role/source URL, run, upstream bucket,
  decision, and write-gate provenance. The recovery path for
  `scored_application_not_selected` requires fit >= 7 plus upstream
  `Proceed`/`accepted` evidence and rejects generic growth marketing,
  blocklisted, dropped, and rejected noise. It does not mutate `jobs.xlsx` or the
  company tracker.

# Post-run reminders (2026-07-28 bulk catch-up)

## Must tell Akshat after this run

### 1. Still wasting time on already-pending people
**Symptom:** Chrome spends a long stretch opening people who already show pending/connected.

**Why (not fixed yet):** Track 2 still does a **profile-reconcile** pass over `Invited` / `Invite uncertain` contacts (and orgs with unresolved reservations) before/around invite send. That was added to clear stuck `send_unknown_reserved` slots — but with **604 Invited** contacts it burns a lot of LinkedIn time visiting already-pending profiles. The preflight/`send_unknown` fix released false unknowns; it did **not** stop the reconcile walk over normal pending invites.

**Fix next:** Cap/skip profile reconcile for contacts already `Invited` with a fresh reservation status of `sent` / `reconciled_pending` — only open profiles for true unresolved unknowns. Prefer DM/thread reconcile over profile opens for accepted people.

### 2. Bulk budget / reliability (partially done this session)
- Raised default invite target 27→40; catch-up CLI uses **60**.
- Preflight/`chrome-error://` no longer freezes slots as `send_unknown_reserved` (retryable `preflight_failed`).
- Released ~20 false unknown reservations from last night.
- Worker timeout 60→90s; more chrome-error preflight retries.

### 3. Check after run
- Confirmed **sent** count vs selected (not just slots reserved).
- Whether profile-reconcile still dominated runtime before first new invite.
- Remaining `send_unknown_reserved` count in `workspace/linkedin_invite_send_reservations.json`.

### 4. 2026-07-28 morning status (bulk catch-up)
- Process **not running** (last artifacts ~03:58).
- **No** `track-2-daily-run.json` written → run never completed.
- Confirmed invites sent in this bulk attempt: **0**.
- Plan had budgeted ~58 invite slots / 21 companies, but only Collectly hit invite phase (`protected_review: 2`, 0 sends).
- Multiple restart/monitor attempts; stdout logs stayed nearly empty (node deprecation only).

### 4. Invite selection ordered ledger filter after limit (FIXED this session)
**Symptom:** Companies with live search hits still sent 0 — top candidate already sent/connected emptied the slot.
**Fix:** Rank full eligible pool → drop reservation/protected blocks → then apply per-company cap (`cli.py` track-2 invite phase).


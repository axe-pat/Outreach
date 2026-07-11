# LinkedIn Browser Session Playbook

This playbook is the single source of truth for the Chrome / LinkedIn session used by both:

- `Outreach`
- `ResumeGenerator v1` live LinkedIn discovery

The point of this document is simple: do not rediscover the same browser-profile problem again.

## Canonical Rules

1. Use one explicit signed-in Chrome profile.
2. On this machine, the verified working profile is:

```text
/Users/akshat/Desktop/Claude projects/Outreach/playwright/chrome-data
```

3. Always launch that profile with:
   - `--remote-debugging-port=9222`
   - `--enable-automation`
   - `--disable-extensions`
4. Verify `9222` before running any LinkedIn automation.
5. Prefer CDP attach flows once Chrome is already live on `9222`.
6. Do not rely on repo-relative `playwright/chrome-data` defaults.

## Why This Breaks

This issue has shown up in two different ways:

1. Wrong profile
The code or shell points at a repo-local fallback profile instead of the known signed-in one.

2. Wrong launch mode
Chrome is open, but not with remote debugging on `9222`, so automation cannot attach.

3. Wrong attach semantics
Some commands launch a separate persistent browser instead of attaching to the already-good Chrome session.

4. Chrome 147+ CDP quirk
If Chrome is launched on `9222` without `--enable-automation`, Playwright CDP attach can fail with:

```text
Protocol error (Browser.setDownloadBehavior): Browser context management is not supported.
```

That error is not a mystery anymore. It means the running Chrome session is not in the launch mode Playwright expects for this workflow.

5. Extension service workers
Chrome extensions can attach service workers to the CDP session and crash
Playwright with an error shaped like:

```text
targetInfo: { "type": "service_worker", "url": "chrome-extension://..." }
BrowserType.connect_over_cdp: Connection closed while reading from the driver
```

For automation runs, relaunch the Outreach browser with `--disable-extensions`.

## Verified Launch Pattern

Use this exact shape when launching manually:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --user-data-dir="/Users/akshat/Desktop/Claude projects/Outreach/playwright/chrome-data" \
  --remote-debugging-port=9222 \
  --enable-automation \
  --disable-extensions \
  https://www.linkedin.com/feed/
```

Or use the repo launchers, which should now encode the same behavior.

## Required Verification

Before running discovery or outreach, verify both of these:

```bash
lsof -nP -iTCP:9222 -sTCP:LISTEN
curl -s http://127.0.0.1:9222/json/version
```

Expected outcome:

- `lsof` shows Chrome listening on `127.0.0.1:9222`
- `curl` returns JSON with a `webSocketDebuggerUrl`

If either check fails, stop and relaunch Chrome correctly.

## Safe Project Flows

### Outreach

```bash
cd "/Users/akshat/Desktop/Claude projects/Outreach"
export LINKEDIN_CHROME_USER_DATA_DIR="/Users/akshat/Desktop/Claude projects/Outreach/playwright/chrome-data"
./scripts/launch_outreach_browser.sh
./.venv/bin/python main.py check-linkedin-live
```

Then proceed with:

- `python main.py run ...`
- `python main.py send-invites ...`

Invite sending has two gates by default:

- note QC verdict must be `send`
- candidate relevance score must be at least `35`

Use `--min-score` to tighten or loosen that candidate gate. A dry-run
`send-invites` checks the LinkedIn flow and writes artifacts, but does not add
contacts or touchpoints to the workspace; tracking is written only with
`--execute`.

#### Cockpit-reviewed one-row execution

The operator cockpit must not call the batch commands directly. Use the
review-bound module so the artifact and the exact human-reviewed row cannot
change between review and execution:

```bash
# 1. Render the exact proposal shown to the reviewer.
./.venv/bin/python -m outreach.reviewed_linkedin preview \
  --action followup \
  --source-artifact /absolute/path/to/linkedin-followup-drafts.json \
  --row-index 0 \
  --output /absolute/path/to/proposal.json

# 2. After review, bind the proposal digest into a create-once approval.
./.venv/bin/python -m outreach.reviewed_linkedin approve \
  --action followup \
  --source-artifact /absolute/path/to/linkedin-followup-drafts.json \
  --row-index 0 \
  --expect-proposal-sha256 PROPOSAL_SHA256 \
  --approved-by local-owner \
  --approval-file /absolute/path/to/approval.json

# 3. Execute exactly that row. The approval is consumed before send code runs.
./.venv/bin/python -m outreach.reviewed_linkedin execute \
  --approval-file /absolute/path/to/approval.json \
  --expect-approval-sha256 APPROVAL_SHA256 \
  --receipt-file /absolute/path/to/receipt.json \
  --execute
```

Use `--action invite` for invite artifacts. If the operator edits the outgoing
copy, write it to a UTF-8 file and pass the same `--outgoing-message-file` to
both `preview` and `approve`. The approval binds the source artifact SHA256,
row index, canonical recipient/profile or thread/contact, company, full latest
inbound context for follow-ups, and exact outgoing text. Approval and receipt
files are create-once. The execution ledger is locked and written atomically;
once reserved, an approval cannot be replayed even when delivery becomes
unknown and needs signed-in reconciliation.

Follow-up approval requires a real, non-synthetic LinkedIn `thread_id`; the
reviewed sender never falls back to a name match. Immediately before the live
call it re-runs the same tracker-backed cadence, duplicate, stop, and learned-
negative guard used by the public follow-up command. Execution uses the row
snapshot inside the immutable approval, not the mutable source file. The one
canonical replay ledger lives under the configured tracker workspace and is
not caller-selectable. CLI stdout contains status and digests only; the full
PII-bearing proposal and receipt are written to their explicitly requested
files. Only exactly one literal `sent` result is complete; every other result
is blocked or unknown and marked for reconciliation.

### ResumeGenerator v1 discovery

```bash
cd "/Users/akshat/Desktop/Claude projects/ResumeGenerator v1"
export LINKEDIN_CHROME_USER_DATA_DIR="/Users/akshat/Desktop/Claude projects/Outreach/playwright/chrome-data"
./discovery/scripts/launch_linkedin_browser.sh
./discovery/scripts/check_linkedin_live.sh
```

Then proceed with:

- `python discovery/auto/linkedin_live.py ...`

## What To Avoid

- Do not assume “Chrome is open” means CDP is available.
- Do not run LinkedIn automation before verifying `9222`.
- Do not use a relative `LINKEDIN_CHROME_USER_DATA_DIR`.
- Do not point discovery at `ResumeGenerator v1/discovery/playwright/chrome-data`.
- Do not point outreach at an unverified fallback profile.
- Do not treat `prepare-browser` or other persistent-context flows as the default path when a good CDP session is already live.

## Fast Recovery Checklist

If the session breaks:

1. Check port `9222`.
2. If nothing is listening, relaunch Chrome with the canonical profile and `--enable-automation`.
3. If something is listening, inspect the owner:

```bash
ps -p "$(lsof -tiTCP:9222 -sTCP:LISTEN)" -o command=
```

4. Confirm the command includes:
   - the canonical profile path
   - `--remote-debugging-port=9222`
   - `--enable-automation`
   - `--disable-extensions`
5. Re-run the project-specific live check.

## If You See These Symptoms

### Symptom
Chrome window opens, but the workflow behaves like a fresh/unsigned profile.

### Meaning
Wrong user-data-dir or a persistent automation window was launched instead of attaching to the known good session.

### Fix
Relaunch with the canonical profile path and verify `9222`.

### Symptom
`check-linkedin-live` says nothing is listening on `9222`.

### Meaning
Chrome is open, but not in debug mode.

### Fix
Relaunch with `--remote-debugging-port=9222`.

### Symptom
Playwright fails with `Browser.setDownloadBehavior` / `Browser context management is not supported`.

### Meaning
Chrome was launched on `9222` without the automation-compatible mode that this workflow needs.

### Fix
Relaunch with `--enable-automation`.

### Symptom
Playwright fails with a `targetInfo` service-worker assertion for a
`chrome-extension://...` URL.

### Meaning
An extension in the Chrome profile attached a service worker to CDP and broke
Playwright's attach flow.

### Fix
Stop the dedicated `9222` Chrome and relaunch through
`scripts/launch_outreach_browser.sh`, which disables extensions.

## Permanent Team Rule

For this machine and these two repos, treat the Chrome session as shared infrastructure:

- one signed-in profile
- one debug port
- verified before automation
- CDP attach first

If a future change breaks any of those rules, update this playbook before normalizing the new behavior.

# LinkedIn Browser Session Playbook

This playbook is the single source of truth for the Chrome / LinkedIn session used by both:

- `Outreach`
- `ResumeGenerator v1` live LinkedIn discovery

The point of this document is simple: do not rediscover the same browser-profile problem again.

## Canonical Rules

1. Use one explicit signed-in Chrome profile.
2. On this machine, the verified working profile is:

```text
/Users/akshat/Desktop/Claude Projects/Outreach/playwright/chrome-data
```

3. Always launch that profile with:
   - `--remote-debugging-port=9222`
   - `--enable-automation`
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

## Verified Launch Pattern

Use this exact shape when launching manually:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --user-data-dir="/Users/akshat/Desktop/Claude Projects/Outreach/playwright/chrome-data" \
  --remote-debugging-port=9222 \
  --enable-automation \
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
cd "/Users/akshat/Desktop/Claude Projects/Outreach"
export LINKEDIN_CHROME_USER_DATA_DIR="/Users/akshat/Desktop/Claude Projects/Outreach/playwright/chrome-data"
./scripts/launch_outreach_browser.sh
./.venv/bin/python main.py check-linkedin-live
```

Then proceed with:

- `python main.py run ...`
- `python main.py send-invites ...`

### ResumeGenerator v1 discovery

```bash
cd "/Users/akshat/Desktop/Claude Projects/ResumeGenerator v1"
export LINKEDIN_CHROME_USER_DATA_DIR="/Users/akshat/Desktop/Claude Projects/Outreach/playwright/chrome-data"
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

## Permanent Team Rule

For this machine and these two repos, treat the Chrome session as shared infrastructure:

- one signed-in profile
- one debug port
- verified before automation
- CDP attach first

If a future change breaks any of those rules, update this playbook before normalizing the new behavior.

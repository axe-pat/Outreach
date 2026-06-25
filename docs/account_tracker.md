# Account Tracker — Operational Reference

Scoring design decisions live in `docs/relationship_engine.md` → **Scoring Philosophy**.

## How To Run

```bash
outreach account-tracker
# with explicit paths:
outreach account-tracker --workspace workspace --output workspace/account_tracker.xlsx
```

## Output

`workspace/account_tracker.xlsx` — three sheets:

- **Account Tracker** — all companies, full detail, auto-filter on every column
- **Tier A — Active Campaign** — top 20 companies
- **Action Queue** — Tier A/B companies with actionable stages, sorted by urgency
  (conversation → connected → active outreach → people mapped → priority target)

## Key Columns

| Column | Notes |
|--------|-------|
| Fit Score | 0–100, sum of profile fit + role fit + team size gate + reachability + hiring likelihood |
| Tier | A (top 20) / B (next 40) / C (rest) — rank-based, not threshold |
| Account Stage | Derived from contact/touchpoint state — see relationship_engine.md |
| Why Fit | Top signals that drove the score |
| Next Action | Recommended move based on account stage |
| Score: Profile / Role / Reach / Hiring | Component breakdown for transparency |

## Source Files

| File | Purpose |
|------|---------|
| `src/outreach/account_tracker.py` | All scoring and Excel generation logic |
| `workspace/account_tracker.xlsx` | Generated output — regenerate freely, not a source of truth |
| `docs/relationship_engine.md` | Scoring philosophy and design decisions |

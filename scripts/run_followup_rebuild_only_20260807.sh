#!/usr/bin/env bash
# Rebuild cadence + tiered backlog + drafts from current tracker (no live LinkedIn).
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
REPO_DIR="/Users/akshat/Desktop/Claude projects/Outreach"
cd "$REPO_DIR"
export PYTHONUNBUFFERED=1
LOG="artifacts/20260807-followup-rebuild-only.log"
: > "$LOG"
exec > >(tee -a "$LOG") 2>&1
echo "RUN_START=$(date -Iseconds)"
caffeinate -dimsu -w $$ &
CAFFEINE_PID=$!
trap 'kill $CAFFEINE_PID 2>/dev/null || true' EXIT

./.venv/bin/python <<'PY'
import csv, json, re, subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from outreach.config import OutreachSettings
from outreach.tracking import OutreachWorkbook
from outreach.style_profile import load_style_profile_if_exists
from outreach.cli import (
    build_linkedin_followup_drafts,
    build_runtime_ai_messaging,
    write_artifact,
)

settings = OutreachSettings()
wb = OutreachWorkbook(settings.resolved_tracking_workspace_dir)

print("Building cadence...", flush=True)
subprocess.run(["./.venv/bin/python", "main.py", "build-outreach-cadence-report"], check=True)
cadence_path = sorted(Path("artifacts").glob("*outreach-cadence-report.json"))[-1]
print("Cadence:", cadence_path, flush=True)
rep = json.loads(cadence_path.read_text())

orgs = {o.organization_id: o for o in wb.list_organizations()}
contacts = {c.contact_id: c for c in wb.list_contacts()}
touchpoints = wb.list_touchpoints()

fall = {}
with open("workspace/fall_sprint_targets.csv", newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        fall[row["company"].strip().lower()] = row

def fall_for_org(org):
    if not org:
        return None
    name = (org.name or "").strip().lower()
    if name in fall:
        return fall[name]
    for k, v in fall.items():
        if k and (k in name or name in k):
            return v
    return None

FOUNDER_EXEC = re.compile(r"\b(founder|co[- ]?founder|ceo|cto|cpo|coo|chief|vp\b|vice president|head of|founding)\b", re.I)
PM_OPS = re.compile(r"\b(product manager|\bpm\b|product lead|product ops|product operation|forward[- ]deployed|gtm|strategy|strategic|growth)\b", re.I)
ENGINEER_ONLY = re.compile(r"\b(software engineer|swe\b|sde\b|engineer|developer|sdet|embedded|ios |android |flutter|compiler)\b", re.I)
HIGH_ROLE = re.compile(r"\b(founder|co[- ]?founder|ceo|cto|cpo|coo|chief|vp|vice president|head of|director|product manager|pm\b|product lead|product ops|forward[- ]deployed|growth|talent|recruiting|people ops|hiring|gtm|strategy|strategic)\b", re.I)
MEH_ROLE = re.compile(r"\b(engineer|software|sde|swe|developer|designer|account exec|sales rep|recruiter coordinator|hr business|events|marketing coordinator|brand)\b", re.I)

def role_score(title, contact_type=""):
    t = f"{title or ''} {contact_type or ''}"
    if HIGH_ROLE.search(t):
        return 2
    if MEH_ROLE.search(t) and not HIGH_ROLE.search(t):
        return 0
    return 1

def triage_bucket(contact):
    notes = (contact.notes or "").lower()
    status = (contact.status or "").lower()
    if status in {"skipped", "do_not_contact", "replied"}:
        return status
    if "suppress follow-up" in notes or ("park" in notes and "triage" in notes):
        return "parked_triage" if "park" in notes else "suppress_triage"
    if "play (user triage)" in notes:
        if "park" in notes:
            return "parked_triage"
        if "suppress" in notes:
            return "suppress_triage"
        if "rewrite" in notes:
            return "rewrite_first"
    org = orgs.get(contact.organization_id)
    if org:
        tags = (org.target_lists or "").lower()
        onotes = (org.notes or "").lower()
        if "do_not_pursue" in tags or "do_not_pursue" in onotes:
            return "org_do_not_pursue"
    return None

def retier(r):
    band = r.get("band") or ""
    title = r.get("title") or ""
    triage = r.get("triage") or ""
    score = float(r.get("fall_score") or 0)
    if triage in {"parked_triage", "suppress_triage"} or band == "parked_large":
        return "T4_parked"
    if band == "out_of_scope" and score < 20:
        if FOUNDER_EXEC.search(title) or PM_OPS.search(title):
            return "T3_solid"
        return "T4_lower"
    founder = bool(FOUNDER_EXEC.search(title))
    pm = bool(PM_OPS.search(title))
    eng = bool(ENGINEER_ONLY.search(title)) and not founder and not pm
    if band == "S_seed_proven" and (founder or pm or not eng):
        return "T1_highest"
    if band == "A_warm" and (founder or pm) and score >= 25:
        return "T1_highest"
    if band in {"B_primary", "B_apply_target"} and founder and score >= 35:
        return "T1_highest"
    if band in {"B_primary", "B_apply_target"} and pm and score >= 38:
        return "T1_highest"
    if band == "A_warm" and score >= 20:
        return "T2_strong"
    if band in {"B_primary", "B_apply_target"} and (founder or pm) and score >= 25:
        return "T2_strong"
    if triage == "rewrite_first" or "rewrite" in triage:
        return "T2_strong"
    if band in {"B_primary", "B_apply_target"} and score >= 35 and not eng:
        return "T2_strong"
    if band in {"B_primary", "B_apply_target", "C_verify_remote", "A_warm", "S_seed_proven"} or r.get("band") == "fall_sprint":
        return "T3_solid"
    if founder or pm:
        return "T3_solid"
    return "T4_lower"

invite_note = {}
invite_at = {}
followup_sent = Counter()
for t in touchpoints:
    cid = t.contact_id
    if t.message_kind == "linkedin_invite":
        body = getattr(t, "message_body", None) or ""
        notes = t.notes or ""
        text = body or notes
        m = re.search(r"invite_note=(.+?)(?:\s*\|\s*|$)", notes)
        if m:
            text = m.group(1)
        if text and (cid not in invite_note or (t.sent_at or "") > (invite_at.get(cid) or "")):
            invite_note[cid] = text.strip()[:2000]
            invite_at[cid] = t.sent_at or t.recorded_at or ""
    if t.message_kind == "linkedin_followup":
        ok = (t.status or "").lower() == "sent" or "send_result=sent" in (t.notes or "")
        if ok or (t.status or "").lower() in {"sent", "recorded"}:
            followup_sent[cid] += 1

due = [r for r in rep["results"] if r.get("state") == "due" and r.get("action") == "linkedin_followup_1"]
pending_path = Path("workspace/linkedin_followup_pending_review.json")
pending = json.loads(pending_path.read_text()) if pending_path.exists() else {"results": []}
pending_by_cid = {r["contact_id"]: r for r in pending.get("results") or []}

rows = []
seen = set()
for r in due:
    cid = r["contact_id"]
    c = contacts.get(cid)
    if not c:
        continue
    org = orgs.get(c.organization_id)
    ft = fall_for_org(org)
    band = (ft or {}).get("band") or ""
    fall_score = float((ft or {}).get("fall_score") or 0)
    tags = (org.target_lists if org else "") or ""
    is_fall = "fall_sprint" in tags.lower()
    triage = triage_bucket(c)
    if triage in {"skipped", "org_do_not_pursue"}:
        continue
    row = {
        "contact_id": cid,
        "name": c.full_name,
        "title": c.title,
        "company": org.name if org else "?",
        "organization_id": c.organization_id,
        "status": c.status,
        "band": band or ("fall_sprint" if is_fall else "no_fall"),
        "fall_score": fall_score,
        "role_score": role_score(c.title, c.contact_type),
        "due_at": r.get("due_at"),
        "anchor_at": r.get("anchor_at"),
        "invite_note": invite_note.get(cid, ""),
        "prior_followups": followup_sent.get(cid, 0),
        "triage": triage or "",
        "linkedin_url": c.linkedin_url,
        "in_pending_review": cid in pending_by_cid,
        "pending_rec": (pending_by_cid.get(cid) or {}).get("send_recommendation", ""),
        "contact_type": c.contact_type,
        "source": "cadence_due",
    }
    row["tier"] = retier(row)
    rows.append(row)
    seen.add(cid)

for pr in pending.get("results") or []:
    cid = pr.get("contact_id")
    if cid in seen:
        continue
    if pr.get("draft_kind") != "accepted_follow_up":
        continue
    if pr.get("send_recommendation") in {"suppress", "wait_for_trigger"}:
        continue
    c = contacts.get(cid)
    if not c or (c.status or "").lower() not in {"connected", "accepted"}:
        continue
    org = orgs.get(c.organization_id)
    ft = fall_for_org(org)
    band = (ft or {}).get("band") or ""
    fall_score = float((ft or {}).get("fall_score") or 0)
    tags = (org.target_lists if org else "") or ""
    is_fall = "fall_sprint" in tags.lower()
    triage = triage_bucket(c)
    row = {
        "contact_id": cid,
        "name": c.full_name,
        "title": c.title,
        "company": org.name if org else pr.get("company", "?"),
        "organization_id": c.organization_id,
        "status": c.status,
        "band": band or ("fall_sprint" if is_fall else "no_fall"),
        "fall_score": fall_score,
        "role_score": role_score(c.title, c.contact_type),
        "due_at": pr.get("cadence_due_at"),
        "anchor_at": "",
        "invite_note": pr.get("original_invite_note") or invite_note.get(cid, ""),
        "prior_followups": followup_sent.get(cid, 0),
        "triage": triage or "from_pending_review",
        "linkedin_url": c.linkedin_url,
        "in_pending_review": True,
        "pending_rec": pr.get("send_recommendation", ""),
        "contact_type": c.contact_type,
        "source": "pending_review_extra",
    }
    row["tier"] = retier(row)
    rows.append(row)
    seen.add(cid)

tier_order = {"T1_highest": 0, "T2_strong": 1, "T3_solid": 2, "T4_lower": 3, "T4_parked": 4}
rows.sort(key=lambda x: (tier_order.get(x["tier"], 9), -float(x.get("fall_score") or 0), -int(x.get("role_score") or 0), x["company"], x["name"]))

connected = sum(1 for c in contacts.values() if (c.status or "").lower() == "connected")
replied = sum(1 for c in contacts.values() if (c.status or "").lower() == "replied")
print(f"Connected={connected} Replied={replied}", flush=True)
print("Backlog", len(rows), dict(Counter(r["tier"] for r in rows)), flush=True)
print("S_seed in backlog:", [(r["company"], r["name"], r["title"][:40]) for r in rows if r.get("band") == "S_seed_proven"], flush=True)

out = {
    "created_at": datetime.now(timezone.utc).isoformat(),
    "source_cadence_report": str(cadence_path),
    "live_refresh": "inbox_message_reconcile",
    "note": "Profile reconcile aborted (LinkedIn captcha). Inbox reconcile applied 2026-08-07.",
    "connected_total": connected,
    "replied_total": replied,
    "total": len(rows),
    "by_tier": dict(Counter(r["tier"] for r in rows)),
    "tier_defs": {
        "T1_highest": "Highest reward: founders/execs/PMs at A_warm (≥25) or strong B_primary; S_seed when accepted",
        "T2_strong": "Strong next: other A_warm, founder/PM on mid B, rewrite-first warm plays",
        "T3_solid": "Worth a clean follow-up: B/C engineers, weaker titles at OK companies, standout OOS people",
        "T4_lower": "Low priority / weak fit / out_of_scope without leverage",
        "T4_parked": "Parked megacorp band or explicit triage park (Cisco/Cosm/etc.)",
    },
    "results": rows,
}
Path("artifacts/20260807-followup-backlog-tiered.json").write_text(json.dumps(out, indent=2))

lines = [
    "# Follow-up backlog (tiered) — 2026-08-07 (LIVE INBOX REFRESH)",
    "",
    f"Total: **{len(rows)}** | Connected: **{connected}** | Replied: **{replied}**",
    f"Cadence: `{cadence_path}`",
    "",
    "Source: live LinkedIn **message** reconcile (200 threads). Profile pass hit captcha — silent accepts may still be missing.",
    "",
    "## Counts",
    "",
]
for k in tier_order:
    lines.append(f"- **{k}**: {out['by_tier'].get(k, 0)} — {out['tier_defs'][k]}")
for tier in tier_order:
    subset = [r for r in rows if r["tier"] == tier]
    lines += ["", f"## {tier} ({len(subset)})", "", "| Company | Name | Title | Band | Fall |", "|---|---|---|---|---|"]
    for r in subset:
        title = (r["title"] or "").replace("|", "/")[:60]
        lines.append(f"| {r['company']} | {r['name']} | {title} | {r['band']} | {r.get('fall_score', 0):.0f} |")
Path("artifacts/20260807-followup-backlog-tiered.md").write_text("\n".join(lines) + "\n")
print("Wrote tiered backlog md/json", flush=True)

reconcile = [{
    "contact_id": r["contact_id"],
    "organization_id": r["organization_id"],
    "name": r["name"],
    "linkedin_url": r.get("linkedin_url") or "",
    "status": "connected",
    "normalized_status": "connected",
    "needs_follow_up": True,
    "original_invite_note": r.get("invite_note") or "",
    "latest_message": "",
    "last_sender": "",
    "message_window": [],
    "thread_id": f"synthetic:{r['contact_id']}",
    "thread_url": "",
    "detail": "Synthesized from inbox-refreshed cadence-due backlog",
    "state_reason": "cadence_due_backlog_inbox_refresh",
    "tier": r["tier"],
    "band": r.get("band"),
    "fall_score": r.get("fall_score"),
} for r in rows]
reconcile_path = Path("artifacts/20260807-followup-backlog-reconcile.json")
reconcile_path.write_text(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(), "count": len(reconcile), "results": reconcile}, indent=2))

print(f"Drafting {len(reconcile)}...", flush=True)
profile = load_style_profile_if_exists(settings.resolved_tracking_workspace_dir / "communication_style_profile.yml")
ai = build_runtime_ai_messaging(settings, style_profile=profile)
drafts = build_linkedin_followup_drafts(
    reconcile_results=reconcile,
    organizations=wb.list_organizations(),
    contacts=wb.list_contacts(),
    opportunities=wb.list_opportunities(),
    style_profile=profile,
    ai_messaging=ai,
)
tier_by_cid = {r["contact_id"]: r for r in rows}
for d in drafts:
    meta = tier_by_cid.get(d["contact_id"], {})
    d["tier"] = meta.get("tier")
    d["band"] = meta.get("band")
    d["fall_score"] = meta.get("fall_score")
    d["role_score"] = meta.get("role_score")
    d["triage"] = meta.get("triage")
drafts.sort(key=lambda d: (tier_order.get(d.get("tier"), 9), -float(d.get("fall_score") or 0), d.get("company") or "", d.get("name") or ""))

def ai_label(d):
    ai = d.get("ai_messaging") or {}
    if not isinstance(ai, dict):
        return "n/a"
    return str(ai.get("provider") or ai.get("mode") or ai.get("status") or ("used" if ai.get("used") else "n/a"))

artifact = write_artifact(
    settings.artifacts_dir,
    "linkedin-followup-drafts-backlog",
    {
        "source_reconcile": str(reconcile_path),
        "source_backlog": "artifacts/20260807-followup-backlog-tiered.json",
        "live_refresh": "inbox_message_reconcile",
        "count": len(drafts),
        "summary": dict(Counter(str(d.get("draft_kind")) for d in drafts)),
        "recommendation_summary": dict(Counter(str(d.get("send_recommendation")) for d in drafts)),
        "ai_summary": dict(Counter(ai_label(d) for d in drafts)),
        "by_tier": dict(Counter(d.get("tier") for d in drafts)),
        "results": drafts,
    },
)
print("Draft artifact:", artifact, flush=True)

md = [
    "# Follow-up draft pack — 2026-08-07 (LIVE INBOX REFRESH)",
    "",
    f"Total drafts: **{len(drafts)}**",
    f"By tier: {dict(Counter(d.get('tier') for d in drafts))}",
    f"AI: {dict(Counter(ai_label(d) for d in drafts))}",
    "",
    "Inbox-refreshed. Engine still weak — review before send.",
    "",
]
for tier in tier_order:
    subset = [d for d in drafts if d.get("tier") == tier]
    md += [f"## {tier} ({len(subset)})", ""]
    for i, d in enumerate(subset, 1):
        rev = d.get("communication_review") or {}
        md += [
            f"### {i}. {d.get('name')} — {d.get('company')}",
            f"- **Title:** {d.get('title')}",
            f"- **Band/score:** {d.get('band')} / {d.get('fall_score')}",
            f"- **Rec:** {d.get('send_recommendation')} | **Review:** {rev.get('verdict')} ({rev.get('score')})",
            f"- **Invite note:** {(d.get('original_invite_note') or '')[:220]}",
            "",
            "```",
            (d.get("draft_message") or "").strip(),
            "```",
            "",
        ]
Path("artifacts/20260807-followup-drafts-by-tier.md").write_text("\n".join(md) + "\n")
print("T1:", [(d.get("company"), d.get("name")) for d in drafts if d.get("tier") == "T1_highest"], flush=True)
print("REBUILD_DONE", flush=True)
PY

echo "EXIT_CODE=0"
echo "DONE"
echo "RUN_END=$(date -Iseconds)"

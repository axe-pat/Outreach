"""Build the ranked fall_sprint target list.

Combines:
  Pass 1  the hand-seeded high-velocity AI / expert-network lane (workspace/fall_sprint_seed.csv)
  Pass 2  a fall-intern re-rank of the existing account universe (workspace/account_tracker.xlsx)

Fall-intern re-rank differs from the normal Account Score:
  - team-size penalty is RELAXED (small teams are good: founder creates the slot)
  - a QUALITY gate is required (funding/pedigree/brand OR strong profile fit) but only
    pushes weak-signal rows into a needs-enrichment band, never hard-drops good-but-unenriched ones
  - warm path (reachability) and founder-accessibility and hiring velocity are weighted UP
  - prestige-for-its-own-sake is de-weighted (used only as a gate, not a score driver)
  - geography locked to LA + remote-friendly

Output: workspace/fall_sprint_targets.csv  (ranked, all signal columns + band)
Read-only against the tracker; writes one new CSV. Does not touch organizations.csv.
"""
from __future__ import annotations
import csv
import re
from pathlib import Path

import openpyxl

WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"
TRACKER = WORKSPACE / "account_tracker.xlsx"
SEED = WORKSPACE / "fall_sprint_seed.csv"
OUT = WORKSPACE / "fall_sprint_targets.csv"

LA_PAT = re.compile(r"los angeles|santa monica|pasadena|culver|venice|\bla\b|burbank|glendale|long beach|el segundo", re.I)
REMOTE_PAT = re.compile(r"remote", re.I)
SF_PAT = re.compile(r"san francisco|bay area|menlo|mountain view|san jose|palo alto|sunnyvale|oakland|berkeley|redwood", re.I)
NY_PAT = re.compile(r"new york|\bny\b|brooklyn|manhattan", re.I)


def geo_class(city: str) -> str:
    c = (city or "").strip()
    if not c or c == "None":
        return "UNKNOWN"
    if REMOTE_PAT.search(c):
        return "REMOTE"
    if LA_PAT.search(c):
        return "LA"
    if SF_PAT.search(c):
        return "SF"
    if NY_PAT.search(c):
        return "NY"
    return "OTHER"


def parse_team(v) -> int | None:
    if v is None:
        return None
    s = str(v)
    m = re.search(r"\d[\d,]*", s)
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def founder_access_bonus(team: int | None) -> int:
    if team is None:
        return 7  # unknown: assume small/remote-friendly startup
    if team <= 15:
        return 12
    if team <= 50:
        return 10
    if team <= 200:
        return 7
    if team <= 1000:
        return 3
    return 0


def num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


FALL_SIGNAL_PAT = re.compile(
    r"fall|co-?op|off-?cycle|winter|spring\s+intern|part-?time\s+intern",
    re.I,
)
FALL_INTERN_ROLE_PAT = re.compile(
    r"fall.*intern|intern.*fall|co-?op|off-?cycle|winter\s+intern",
    re.I,
)


def has_fall_intern_signal(r: dict) -> bool:
    """Live fall/off-cycle intern path from tracker hiring + role fields."""
    hiring = str(r.get("Hiring Signal") or "")
    why = str(r.get("Why Fit") or "")
    role = str(r.get("Target Role") or "")
    if FALL_SIGNAL_PAT.search(hiring) or FALL_SIGNAL_PAT.search(why):
        return True
    return bool(FALL_INTERN_ROLE_PAT.search(role))


def load_tracker_rows():
    wb = openpyxl.load_workbook(TRACKER, read_only=True)
    ws = wb["Account Tracker"]
    rows = list(ws.iter_rows(values_only=True))
    hdr = list(rows[0])
    idx = {h: i for i, h in enumerate(hdr)}
    out = []
    for r in rows[1:]:
        out.append({h: r[idx[h]] for h in hdr})
    return out


def score_row(r: dict):
    team = parse_team(r.get("Team Size"))
    geo = geo_class(str(r.get("City")))
    profile = num(r.get("Score: Profile"))
    reach = num(r.get("Score: Reach"))
    brand = num(r.get("Score: Brand"))
    acct_hiring = num(r.get("Score: Account Hiring"))
    hiring = num(r.get("Score: Hiring"))
    accepted = num(r.get("Accepted"))
    replies = num(r.get("Replies"))
    people = num(r.get("People Mapped"))
    tags = str(r.get("Tags") or "").lower()
    tlists = str(r.get("Target Lists") or "").lower()

    # quality signal: real funding/pedigree/brand OR strong earned profile fit
    quality = (
        brand > 0
        or profile >= 15
        or any(k in tags for k in ["yc", "seed", "series", "funded", "backed", "venture"])
        or "yc" in tlists
    )

    fscore = 0.0
    fscore += profile                      # earned domain fit, 0-25
    fscore += 1.6 * reach                  # warm path weighted UP
    fscore += founder_access_bonus(team)   # small = good
    fscore += 1.3 * acct_hiring            # hiring velocity
    fscore += 0.4 * hiring
    if accepted > 0 or replies > 0:
        fscore += 8                        # already-warm account: prime
    elif people > 0:
        fscore += 2
    warm_traction = accepted > 0 or replies > 0
    fall_signal = has_fall_intern_signal(r)

    # geography lock: LA + remote-friendly
    if geo in ("LA", "REMOTE"):
        geo_ok = "primary"
    elif geo in ("SF", "UNKNOWN") and (team is None or team <= 200):
        geo_ok = "verify_remote"           # small SF/unknown startup: founder may flex remote
        fscore -= 4
    else:
        geo_ok = "out_of_scope"            # NY/other on-site, or large non-LA

    if not quality and not warm_traction and not fall_signal:
        fscore -= 15                       # push (not drop) weak-signal rows down
    if fall_signal and geo_ok in ("primary", "verify_remote"):
        fscore += 6                        # explicit fall/co-op path is sprint-relevant

    # park very large companies unless they have a live fall path or warm traction
    # (apply + follow-up lane, not founder outreach)
    parked = (
        team is not None
        and team >= 1000
        and not warm_traction
        and not fall_signal
    )

    # Traction and live fall signals override the enrichment gate.
    if parked:
        band = "parked_large"
    elif geo_ok == "out_of_scope":
        band = "out_of_scope"
    elif warm_traction:
        band = "A_warm"
    elif fall_signal and geo_ok in ("primary", "verify_remote"):
        band = "B_apply_target"
    elif not quality:
        band = "needs_enrichment"
    elif geo_ok == "primary":
        band = "B_primary"
    else:
        band = "C_verify_remote"

    return {
        "fall_score": round(fscore, 1),
        "band": band,
        "geo_class": geo,
        "geo_ok": geo_ok,
        "team": team if team is not None else "",
        "quality_signal": "yes" if quality else "no",
        "warm_traction": "yes" if warm_traction else "no",
        "fall_signal": "yes" if fall_signal else "no",
        "parked": parked,
    }


def load_seed():
    out = []
    with SEED.open() as f:
        for r in csv.DictReader(f):
            out.append(r)
    return out


def main():
    tracker = load_tracker_rows()
    ranked = []
    for r in tracker:
        s = score_row(r)
        ranked.append({
            "company": r.get("Company"),
            "bucket": "3_universe",
            "fall_score": s["fall_score"],
            "band": s["band"],
            "stage_team": s["team"],
            "geo_class": s["geo_class"],
            "geo_ok": s["geo_ok"],
            "quality_signal": s["quality_signal"],
            "warm_traction": s["warm_traction"],
            "fall_signal": s["fall_signal"],
            "warm_reach": num(r.get("Score: Reach")),
            "fit": num(r.get("Fit Score")),
            "accepted": num(r.get("Accepted")),
            "replies": num(r.get("Replies")),
            "people_mapped": num(r.get("People Mapped")),
            "target_role": r.get("Target Role"),
            "why_fit": r.get("Why Fit"),
            "city": r.get("City"),
            "website": r.get("Website"),
        })

    # seed rows (Bucket 2): proven high-velocity lane, top priority, verified
    for sd in load_seed():
        pri = (sd.get("priority") or "").lower()
        base = {"core": 95, "priority": 88, "watch": 78}.get(pri, 85)
        remote = "remote" in (sd.get("city") or "").lower()
        ranked.append({
            "company": sd.get("company"),
            "bucket": "2_ai_expert_network",
            "fall_score": base,
            "band": "S_seed_proven",
            "stage_team": sd.get("team_size"),
            "geo_class": "REMOTE" if remote else geo_class(sd.get("city")),
            "geo_ok": "primary",
            "quality_signal": "yes",
            "warm_reach": "",
            "fit": "",
            "accepted": "",
            "replies": "",
            "people_mapped": "",
            "target_role": sd.get("target_roles"),
            "why_fit": sd.get("why_you_have_a_case"),
            "city": sd.get("city"),
            "website": sd.get("website"),
        })

    band_rank = {
        "S_seed_proven": 0,
        "A_warm": 1,
        "B_apply_target": 2,
        "B_primary": 3,
        "C_verify_remote": 4,
        "needs_enrichment": 5,
        "out_of_scope": 6,
        "parked_large": 7,
    }
    ranked.sort(key=lambda x: (band_rank.get(x["band"], 9), -float(x["fall_score"] or 0)))

    cols = [
        "company", "bucket", "band", "fall_score", "stage_team", "geo_class",
        "geo_ok", "quality_signal", "warm_traction", "fall_signal",
        "warm_reach", "fit", "accepted", "replies", "people_mapped",
        "target_role", "why_fit", "city", "website",
    ]
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in ranked:
            w.writerow(row)

    from collections import Counter
    bands = Counter(r["band"] for r in ranked)
    print(f"wrote {OUT} ({len(ranked)} rows)")
    for b in band_rank:
        print(f"  {b:18s} {bands.get(b,0)}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from outreach.style_profile import CommunicationStyleProfile


SLOP_PHRASES = [
    "i hope this email finds you well",
    "i am writing to express",
    "i was impressed by",
    "your work stood out",
    "pick your brain",
    "quick coffee chat",
    "would love to connect",
    "passionate about",
    "at the intersection of",
    "leverage my skills",
    "unique blend",
    "synergy",
    "dynamic",
    "innovative company",
    "i believe my background makes me a strong fit",
    "would love your perspective",
    "could translate",
    "product/operator paths",
    "product/operator work",
    "tight resume + 3-line blurb",
    "3-line blurb",
    "route i should understand",
    "where my engineering + marshall background",
]

HUMAN_MARKERS = [
    "blunt",
    "concrete",
    "not just",
    "not trying",
    "directionally",
    "useful",
    "if not",
    "actually",
    "I know",
]


@dataclass
class CommunicationSourceSummary:
    path: str
    source_type: str
    weight: str
    items: int
    notes: str = ""


@dataclass
class EmailCraftReview:
    score: int
    verdict: str
    flags: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)


@dataclass
class OutreachMessageReview:
    score: int
    verdict: str
    channel: str
    flags: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    banned_phrases: list[str] = field(default_factory=list)
    rewrite_guidance: list[str] = field(default_factory=list)
    quality_labels: list[str] = field(default_factory=list)
    recommended_action: str = "review"


def build_communication_lab(
    *,
    workspace: Path,
    repo_root: Path,
    resume_root: Path | None = None,
) -> dict[str, object]:
    """Build a corpus-backed craft brief for high-stakes outreach."""
    sources = collect_communication_sources(workspace=workspace, repo_root=repo_root, resume_root=resume_root)
    return {
        "purpose": (
            "Make outreach feel written by a specific person with a specific reason, "
            "not generated from a networking template."
        ),
        "source_summary": [source.__dict__ for source in sources],
        "stellar_email_principles": stellar_email_principles(),
        "human_signals": [
            "Names the actual thing learned, noticed, built, or misunderstood.",
            "Admits the ask might not fit instead of pretending certainty.",
            "Uses a concrete callback or story mechanism rather than admiration.",
            "Makes the recipient's job easy: reply with yes, no, or the right route.",
            "Sounds like a person thinking on the page, not a brochure about themselves.",
        ],
        "slop_flags": SLOP_PHRASES,
        "draft_shape": [
            "Open with a precise reason for writing.",
            "Add one earned story-fit line from Akshat's actual background.",
            "Include one human/friction line that makes the note feel real.",
            "Ask for a concrete next step and make a no acceptable.",
        ],
        "review_contract": [
            "Every generated message gets a score, verdict, flags, strengths, and recommended action.",
            "The generator does not send from taste alone; review metadata must travel with the draft.",
            "LinkedIn follow-ups can be shorter, but still need a concrete ask or next move.",
            "Emails need an earned story-fit line and a human/friction line.",
        ],
    }


def collect_communication_sources(
    *,
    workspace: Path,
    repo_root: Path,
    resume_root: Path | None = None,
) -> list[CommunicationSourceSummary]:
    sources: list[CommunicationSourceSummary] = []
    coffee_dump = repo_root / "Consulting" / "resources" / "Coffee chat dump.txt"
    if coffee_dump.exists():
        text = coffee_dump.read_text(encoding="utf-8", errors="ignore")
        sources.append(
            CommunicationSourceSummary(
                path=str(coffee_dump),
                source_type="gold_user_written_coffee_chat",
                weight="high",
                items=_count_message_like_blocks(text),
                notes="Rough, human, specific follow-up writing; strongest source for authentic callbacks.",
            )
        )
    touchpoints = workspace / "touchpoints.csv"
    if touchpoints.exists():
        sent_count = 0
        with touchpoints.open(encoding="utf-8", errors="ignore", newline="") as handle:
            for row in csv.DictReader(handle):
                if (row.get("message_text") or "").strip():
                    sent_count += 1
        sources.append(
            CommunicationSourceSummary(
                path=str(touchpoints),
                source_type="silver_sent_outreach",
                weight="medium",
                items=sent_count,
                notes="Real sent LinkedIn follow-ups; useful for what passed review, but short-form and repetitive.",
            )
        )
    if resume_root:
        for rel_path in [
            "docs/reference/STORY_BANK_RICH.md",
            "docs/career_workbench/story_sources/interview_story_scripts.md",
            "docs/career_workbench/answer_engine.md",
            "resume/freeform/prompts/freeform_voice_rewrite.txt",
        ]:
            path = resume_root / rel_path
            if path.exists():
                sources.append(
                    CommunicationSourceSummary(
                        path=str(path),
                        source_type="story_material",
                        weight="medium",
                        items=_count_headings(path.read_text(encoding="utf-8", errors="ignore")),
                        notes="Story mechanisms and details to pull into earned outreach claims.",
                    )
                )
    return sources


def review_outreach_message(
    *,
    body: str,
    subject: str = "",
    channel: str,
    company: str = "",
    recipient_type: str = "general",
    recipient_title: str = "",
    style_profile: CommunicationStyleProfile | None = None,
    grounding_context: str = "",
) -> OutreachMessageReview:
    text = f"{subject}\n{body}".strip()
    lower = text.lower()
    flags: list[str] = []
    strengths: list[str] = []
    banned_phrases: list[str] = []
    score = 100

    normalized_channel = channel.strip().lower().replace("-", "_") or "general"
    if normalized_channel == "email":
        if len(body) < 450:
            flags.append("Too thin for a high-stakes cold email")
            score -= 12
        elif len(body) <= 1400:
            strengths.append("Enough room for a real argument without becoming an essay")
        else:
            flags.append("Too long for first touch")
            score -= 12
    elif normalized_channel == "linkedin_invite":
        if len(body) > 300:
            flags.append("LinkedIn invite note is over 300 chars")
            score -= 50
        elif len(body) < 120:
            flags.append("LinkedIn invite note may be too thin")
            score -= 8
        else:
            strengths.append("LinkedIn invite length is usable")
    elif normalized_channel == "linkedin_followup":
        if len(body) > 900:
            flags.append("LinkedIn follow-up is too long")
            score -= 18
        elif len(body) < 45:
            flags.append("LinkedIn follow-up may be too thin")
            score -= 8
        else:
            strengths.append("LinkedIn follow-up length is usable")
    else:
        if not body.strip():
            flags.append("Message is empty")
            score -= 60

    banned_hits = [phrase for phrase in SLOP_PHRASES if phrase in lower]
    if style_profile is not None:
        banned_hits.extend(style_profile.banned_phrases_in(text))
    banned_hits = sorted(set(banned_hits))
    for phrase in banned_hits:
        flags.append(f"Slop phrase: {phrase}")
        banned_phrases.append(phrase)
        score -= 16
    if not banned_hits:
        strengths.append("Avoids obvious AI/networking-template phrases")

    if company and company.lower() in lower:
        strengths.append("Names the company directly")
    elif normalized_channel in {"email", "linkedin_followup"}:
        flags.append("Does not name the company clearly")
        score -= 10

    has_earned_background = bool(
        re.search(
            r"\b(hevo|gojek|intuit|optum|marshall|mba|data/platform|backend|marketplace|workflow|recruiting systems|technical pm|former engineer)\b",
            lower,
        )
    )
    if normalized_channel == "email":
        if has_earned_background:
            strengths.append("Uses earned Akshat-specific background")
        else:
            flags.append("Missing earned Akshat-specific background")
            score -= 14
    elif has_earned_background:
        strengths.append("Has a real background anchor")

    has_human_friction = bool(
        re.search(
            r"\b(if not|if this is off|blunt no|quick no|not trying|not just|directionally|actually useful|forcing the fit|over-ask|cold emails|from the outside|wrong process|off-base)\b",
            lower,
        )
    )
    if has_human_friction:
        strengths.append("Has a human/friction line")
    elif normalized_channel == "email":
        flags.append("Missing human/friction line")
        score -= 14
    elif normalized_channel == "linkedin_followup" and not has_earned_background:
        flags.append("Clean but lacks earned/human texture beyond the ask")
        score -= 8

    has_concrete_ask = bool(
        re.search(
            r"\b(pointer|route|right person|send a .*blurb|resume|quick read|worth|who owns|who should i ask|any recs on who|hiring-team|hiring team|referral|radar|short blurb|would you suggest|could i send|do you think|does that background|open to|perspective|next contact|best next contact)\b",
            lower,
        )
    )
    if not has_concrete_ask:
        flags.append("Ask is not concrete enough")
        score -= 12
    else:
        strengths.append("Concrete low-friction ask")

    title_lower = recipient_title.lower()
    senior_title = any(
        token in title_lower
        for token in ["principal", "staff", "director", "head of", "vp ", "vice president", "chief ", "cto"]
    )
    tactical_referral_ask = bool(
        re.search(r"\b(tight resume|3-line blurb|three-line blurb|referral path|hiring contact)\b", lower)
    )
    if normalized_channel == "linkedin_followup" and senior_title and tactical_referral_ask:
        flags.append("Seniority mismatch: tactical referral ask to senior/principal contact")
        score -= 18

    if "product/operator" in lower:
        flags.append("Blurry path framing: use product unless operator is the actual target")
        score -= 12
    if "engineering + marshall" in lower:
        flags.append("Credential phrasing: use engineering + MBA unless USC/Marshall warmth is the actual reason")
        score -= 8
    if "would love your perspective" in lower or "could translate" in lower:
        flags.append("Soft translation ask: make the yes/no ask obvious on first read")
        score -= 10
    if re.search(r"\b(angle|route)\b.*\b(understand|make sense)\b", lower):
        flags.append("High-cognitive-load ask: replace angle/route wording with does this fit and any recs on who to talk to")
        score -= 10

    if lower.count("i ") + lower.count("i'm") > 12:
        flags.append("Too self-centered")
        score -= 8

    if re.search(r"\b(profound|stellar|authentic|human|4th wall|ai slop)\b", lower):
        flags.append("Talks about being authentic instead of being useful")
        score -= 10

    context_lower = grounding_context.lower()
    unsupported_callbacks = [
        ("small-team/high-ownership", ["small team", "high ownership", "high-ownership"]),
        ("customer-feedback loop", ["customer feedback", "feedback loop"]),
        ("exactly what i'm looking for", ["exactly what", "looking for"]),
    ]
    for phrase, evidence_terms in unsupported_callbacks:
        if phrase in lower and not any(term in context_lower for term in evidence_terms):
            flags.append(f"Unsupported callback: {phrase}")
            score -= 18

    generic_insight_patterns = [
        (
            r"feels like an early team where product, ops, and execution sit close",
            "Generic company insight: early-team/operator line needs real evidence",
        ),
        (
            r"technically deep product work feels close to problems i've worked around",
            "Generic company insight: technical-product line needs a sharper company-specific mechanism",
        ),
        (
            r"developer-facing product work feels close to problems i've worked around",
            "Generic company insight: developer-product line needs a sharper company-specific mechanism",
        ),
        (
            r"trying to understand where my engineering \+ marshall background could fit",
            "Generic fit framing: asks where the background fits without naming the company-specific angle",
        ),
        (
            r"exploring product/operator paths",
            "Blurry path framing: product/operator should collapse to product unless operator is intentional",
        ),
        (
            r"would love your perspective on whether my background could translate",
            "Soft translation ask: recipient has to decode the ask",
        ),
    ]
    for pattern, flag in generic_insight_patterns:
        if re.search(pattern, lower):
            flags.append(flag)
            score -= 10

    rewrite_guidance = build_rewrite_guidance(
        flags=flags,
        channel=normalized_channel,
        recipient_type=recipient_type,
        recipient_title=recipient_title,
    )

    score = max(0, min(100, score))
    if score >= 88 and not flags:
        verdict = "strong_send_candidate"
        recommended_action = "send_candidate"
    elif score >= 76:
        verdict = "review"
        recommended_action = "human_review"
    else:
        verdict = "needs_rewrite"
        recommended_action = "rewrite_before_send"
    return OutreachMessageReview(
        score=score,
        verdict=verdict,
        channel=normalized_channel,
        flags=flags,
        strengths=strengths,
        banned_phrases=banned_phrases,
        rewrite_guidance=rewrite_guidance,
        quality_labels=classify_quality_labels(
            flags=flags,
            strengths=strengths,
            channel=normalized_channel,
            recommended_action_hint=recommended_action,
        ),
        recommended_action=recommended_action,
    )


def review_email_craft(subject: str, body: str, *, company: str = "", recipient_type: str = "general") -> EmailCraftReview:
    review = review_outreach_message(
        subject=subject,
        body=body,
        channel="email",
        company=company,
        recipient_type=recipient_type,
    )
    return EmailCraftReview(
        score=review.score,
        verdict=review.verdict,
        flags=review.flags,
        strengths=review.strengths,
    )


def stellar_email_principles() -> list[str]:
    return [
        "Specific beats polished: one exact reason is worth ten adjectives.",
        "The note should contain a falsifiable claim about fit, not a vibe.",
        "A little self-awareness is good; theatrical self-awareness is still slop.",
        "The recipient should be able to answer without doing emotional labor.",
        "Use story mechanisms from Akshat's real work: alert storms, latency cliffs, supply APIs, billing/workflow systems.",
        "Make no an acceptable answer. That is what makes the ask feel real.",
    ]


def build_rewrite_guidance(
    *,
    flags: list[str],
    channel: str,
    recipient_type: str = "general",
    recipient_title: str = "",
) -> list[str]:
    """Translate review flags into concrete rewrite directions."""
    guidance: list[str] = []

    def add(text: str) -> None:
        if text not in guidance:
            guidance.append(text)

    flag_text = "\n".join(flags).lower()
    if "unsupported callback" in flag_text:
        add("Remove the callback unless it is explicitly supported by the contact's message; thank them and make the next ask directly.")
    if "slop phrase" in flag_text or "banned phrase" in flag_text:
        add("Replace networking-template phrasing with the exact reason for this person or company.")
    if "missing earned akshat-specific background" in flag_text:
        add("Add one earned background anchor: FlairX, Hevo, Gojek, Intuit, Optum, Marshall, or the recruiting/workflow engine.")
    if "missing human/friction line" in flag_text:
        add("Add a small friction line that makes a no acceptable instead of overselling the fit.")
    if "ask is not concrete enough" in flag_text:
        add("End with one easy answer: route me, quick read, permission to send a short blurb, or no.")
    if "seniority mismatch" in flag_text:
        add("For senior/principal contacts, ask one simple product-fit or right-person question; do not ask them to process a tactical referral packet.")
    if "generic company insight" in flag_text or "generic fit framing" in flag_text:
        add("Replace the generic company-fit line with a specific product, workflow, customer, infrastructure, or operating mechanism.")
    if "too self-centered" in flag_text:
        add("Cut self-summary and give the recipient a clean reason and a clean next action.")
    if channel == "email" and not guidance:
        add("Keep the email specific: reason for writing, earned fit, friction/no-is-ok line, concrete ask.")
    if channel == "linkedin_followup" and not guidance:
        add("Keep the follow-up short, grounded in the thread, and pointed at the next action.")
    if recipient_title and any(token in recipient_title.lower() for token in ["founder", "cto", "chief", "principal", "staff", "director"]):
        add("Match the ask to seniority without adding cognitive load: one clear perspective question is enough.")
    if recipient_type in {"founder", "senior_product"}:
        add("A senior recipient needs one sharp company-specific reason; generic interest is not enough.")
    return guidance


def classify_quality_labels(
    *,
    flags: list[str],
    strengths: list[str],
    channel: str,
    recommended_action_hint: str = "",
) -> list[str]:
    labels: list[str] = []
    text = "\n".join(flags).lower()
    strength_text = "\n".join(strengths).lower()
    if channel:
        labels.append(channel)
    if "unsupported callback" in text:
        labels.append("grounding_risk")
    if "generic company insight" in text or "generic fit framing" in text:
        labels.append("generic_insight")
    if "seniority mismatch" in text:
        labels.append("seniority_mismatch")
    if "slop phrase" in text or "banned phrase" in text:
        labels.append("slop_phrase")
    if "ask is not concrete enough" in text:
        labels.append("weak_ask")
    if "missing earned" in text:
        labels.append("missing_earned_fit")
    if "missing human" in text:
        labels.append("missing_human_friction")
    if "concrete low-friction ask" in strength_text:
        labels.append("concrete_ask")
    if "names the company directly" in strength_text:
        labels.append("company_named")
    if recommended_action_hint:
        labels.append(recommended_action_hint)
    return list(dict.fromkeys(labels))


def _count_message_like_blocks(text: str) -> int:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    return sum(1 for block in blocks if re.search(r"\b(Hi|Thanks|thank you|Best|Akshat)\b", block))


def _count_headings(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.startswith("#"))

"""Orchestration: run every thread through the five layers."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from ..tracking import ContactRecord, OpportunityRecord, OrganizationRecord  # noqa: F401
from . import compose as compose_layer
from . import critic as critic_layer
from .context import (
    CompanyFacts,
    campaign_goal,
    company_facts,
    resolve_capability,
    role_family_from_invite,
)
from .decide import decide
from .extract import read_thread
from .models import (
    Action,
    Ask,
    Decision,
    ReplyDraft,
    ThreadRead,
    ThreadState,
)
from .proof import ProofBeat, select_proof_beats, used_proof_beats
from .org_identity import WORKS_ELSEWHERE, classify_contact_membership
from .thread import (
    Message,
    last_inbound_text,
    last_outbound_requires_human,
    order_messages,
    original_invite_text,
    resolve_state,
)
from .touches import touch_cap_reached

@dataclass
class ThreadInput:
    contact: ContactRecord
    organization: OrganizationRecord | None
    raw_window: list[dict]
    opportunities: list[OpportunityRecord]
    segment: str = "reply"
    relationship_context: str = ""
    band: str = ""
    invite_sent_at: datetime | None = None
    capture_confidence: str = "full"
    captured_message_count: int = 0
    expected_message_count: int | None = None
    touch_count: int = 0
    reopen_condition_fired: bool = False
    hold_reason: str = ""
    """Layer-0 data issue that must block model reads and composition."""


def run(
    inputs: Iterable[ThreadInput],
    *,
    client: Any | None = None,
    extract_model: str = "claude-haiku-4-5-20251001",
    compose_model: str = "claude-haiku-4-5-20251001",
    pursuit_season: str = "fall",
    now: datetime | None = None,
    proof_beats: list[ProofBeat] | None = None,
    profile_text: str = "",
    initial_batch_sentence_counts: Counter[str] | None = None,
    initial_company_ask_counts: Counter[tuple[str, str]] | None = None,
    initial_proof_beat_counts: Counter[tuple[Ask, str]] | None = None,
    initial_banned_sentences: Iterable[str] | None = None,
) -> list[ReplyDraft]:
    drafts: list[ReplyDraft] = []

    # --- layers 0 to 3, deterministic except the read ---------------------
    staged: list[
        tuple[ThreadInput, list[Message], CompanyFacts, list[ProofBeat], ReplyDraft]
    ] = []
    for item in inputs:
        messages, order_confident = order_messages(
            item.raw_window, invite_sent_at=item.invite_sent_at
        )
        state = resolve_state(
            messages,
            contact_status=item.contact.status,
            contact_notes=item.contact.notes,
            reopen_condition=(
                "" if item.reopen_condition_fired else item.contact.reopen_condition
            ),
        )
        facts = company_facts(item.organization)
        invite_text = original_invite_text(messages)
        hold_reason = item.hold_reason
        if (
            not hold_reason
            and "manual_followup_hold" in (item.contact.notes or "").casefold()
        ):
            hold_reason = (
                "manual follow-up hold in contact ledger; Akshat will reply later"
            )
        if (
            not hold_reason
            and item.organization is not None
            and item.relationship_context != "warm_uninvited_referral"
        ):
            membership = classify_contact_membership(
                item.contact,
                item.organization,
            )
            if membership.classification == WORKS_ELSEWHERE or (
                membership.bound_affiliation_type
                not in {"none", "employment_or_internship", "untyped_current_affiliation"}
            ):
                if membership.bound_affiliation_type not in {
                    "none",
                    "employment_or_internship",
                    "untyped_current_affiliation",
                }:
                    hold_reason = (
                        "org binding unverified: "
                        f"{facts.name} is a {membership.bound_affiliation_type} "
                        "affiliation without routing value"
                    )
                    if membership.named_employer:
                        hold_reason += (
                            "; title names "
                            f"{membership.named_employer} as employment/internship"
                        )
                    else:
                        hold_reason += "; no employment/internship binding is present"
                else:
                    hold_reason = (
                        "org binding unverified: title names "
                        f"{membership.named_employer}, bound to {facts.name}"
                    )
        if not hold_reason and last_outbound_requires_human(messages):
            hold_reason = "last outbound was an apology or correction; human only"
        recipient_context = " ".join(
            [
                item.contact.title or "",
                facts.name,
                facts.description,
                *(opportunity.title for opportunity in item.opportunities),
                *(message.text for message in messages if not message.is_from_us),
            ]
        )
        usable_proof = select_proof_beats(
            proof_beats or [],
            recipient_context=recipient_context,
            limit=3,
        )
        if hold_reason:
            read = ThreadRead()
            role_family = role_family_from_invite(invite_text)
            decision = Decision(
                action=Action.HOLD,
                rule=0,
                reason=hold_reason,
                campaign_track=facts.campaign_track,
                goal_role_family=role_family,
                goal=campaign_goal(
                    facts,
                    role_family=role_family,
                    season=pursuit_season,
                ),
            )
        else:
            read = read_thread(
                messages,
                client=client,
                model=extract_model,
                contact_title=item.contact.title,
                company=facts.name,
                contact_name=item.contact.full_name,
            )
            decision = decide(
                state=state,
                read=read,
                contact=item.contact,
                facts=facts,
                opportunities=item.opportunities,
                order_confident=order_confident,
                capture_confidence=item.capture_confidence,
                inbound_message_count=sum(not message.is_from_us for message in messages),
                touch_count=item.touch_count,
                reopen_condition_fired=item.reopen_condition_fired,
                band=item.band,
                pursuit_season=pursuit_season,
                now=now,
                invite_text=invite_text,
                has_prior_outbound=any(
                    message.is_from_us and message.source != "original_invite"
                    for message in messages
                ),
            )
        if decision.ask in {Ask.NAME, Ask.INTEL}:
            usable_proof = []
        capability = resolve_capability(
            item.contact,
            facts,
            declared=read.capability,
            state=state,
        )
        draft = ReplyDraft(
            contact_id=item.contact.contact_id,
            organization_id=item.contact.organization_id,
            name=item.contact.full_name,
            company=facts.name or item.contact.organization_id,
            title=item.contact.title,
            segment=item.segment,
            thread_state=state,
            read=read,
            decision=decision,
            capability=capability,
            last_message=messages[-1].text if messages else "",
            last_sender=messages[-1].sender if messages else "",
            capture_confidence=item.capture_confidence,
            captured_message_count=item.captured_message_count or len(messages),
            expected_message_count=item.expected_message_count,
            touch_count=max(0, item.touch_count),
            touch_cap_reached=(
                state in {ThreadState.NO_CONTEXT, ThreadState.OUTBOUND_UNANSWERED}
                and touch_cap_reached(
                    item.touch_count,
                    reopen_condition_fired=item.reopen_condition_fired,
                )
            ),
            reopen_condition_fired=item.reopen_condition_fired,
            usable_proof=[beat.beat_id for beat in usable_proof],
        )
        staged.append((item, messages, facts, usable_proof, draft))
        drafts.append(draft)

    # A dry run is decision-only: composition intentionally has no model, so
    # there is no message for the critic to inspect.  Preserve the Layer 3
    # decisions instead of turning every would-compose row into an
    # ``empty_message`` hold.
    if client is None:
        return drafts

    # --- layer 4 and 5, with a live banned-sentence set --------------------
    banned: list[str] = list(initial_banned_sentences or [])
    counts: Counter[str] = Counter(initial_batch_sentence_counts or {})
    company_ask_counts: Counter[tuple[str, str]] = Counter(
        initial_company_ask_counts or {}
    )
    proof_counts: Counter[tuple[Ask, str]] = Counter(initial_proof_beat_counts or {})
    company_ask_bans: dict[str, list[str]] = defaultdict(list)

    for item, messages, facts, usable_proof, draft in staged:
        if not draft.decision.emits_message:
            continue

        company_key = " ".join(facts.name.casefold().split())
        invite_text = original_invite_text(messages)

        proof_for_compose = [
            beat
            for beat in usable_proof
            if proof_counts[(draft.decision.ask, beat.beat_id)]
            < critic_layer.PROOF_BEAT_PRIOR_LIMIT
        ]
        draft.usable_proof = [beat.beat_id for beat in proof_for_compose]
        message, source = compose_layer.compose(
            messages=messages,
            decision=draft.decision,
            read=draft.read,
            name=item.contact.full_name,
            title=item.contact.title,
            company=facts.name,
            facts=facts,
            # Company-specific ask variants must survive compose's bounded
            # banned list even late in a large batch.
            banned=[*company_ask_bans[company_key], *banned],
            usable_proof=proof_for_compose,
            client=client,
            model=compose_model,
            season=pursuit_season,
            relationship_context=item.relationship_context,
        )
        capability = draft.capability

        for attempt in range(2):
            result = critic_layer.review(
                message=message,
                decision=draft.decision,
                read=draft.read,
                capability=capability,
                batch_sentence_counts=counts,
                batch_company_ask_counts=company_ask_counts,
                has_attachment_task=bool(draft.decision.human_tasks),
                proof_beats=proof_beats,
                proof_beat_counts=proof_counts,
                profile_text=profile_text,
                recipient_title=item.contact.title,
                relationship_context=item.relationship_context,
                recipient_name=item.contact.full_name,
                company=facts.name,
                invite_text=invite_text,
                last_inbound_message=last_inbound_text(messages),
            )
            # The critic owns deterministic copy normalization, so every later
            # check, retry seed and artifact sees the exact text under review.
            message = result.normalized_message
            if result.passed or attempt == 1 or client is None:
                break
            # One regeneration, with the failures fed back as extra bans.
            banned.extend(
                critic_layer.batch_repetition_sentences(message, draft.decision)
            )
            if any(
                flag.startswith("repeated_company_ask:")
                for flag in result.flags
            ):
                company_ask_bans[company_key].extend(
                    critic_layer.company_ask_sentences(message, draft.decision)
                )
            rejected_proof_ids = {
                beat.beat_id for beat in used_proof_beats(message, proof_beats or [])
            }
            retry_proof = [
                beat for beat in proof_for_compose
                if beat.beat_id not in rejected_proof_ids
            ]
            message, source = compose_layer.compose(
                messages=messages,
                decision=draft.decision,
                read=draft.read,
                name=item.contact.full_name,
                title=item.contact.title,
                company=facts.name,
                facts=facts,
                banned=[*company_ask_bans[company_key], *banned],
                usable_proof=retry_proof,
                client=client,
                model=compose_model,
                season=pursuit_season,
                relationship_context=item.relationship_context,
            )

        draft.message = message
        draft.compose_source = source
        draft.critic_flags = result.flags
        draft.critic_passed = result.passed
        if not result.passed:
            draft.decision.action = Action.HOLD
            draft.decision.reason = f"critic: {', '.join(result.flags[:3])}"

        for sentence in critic_layer.batch_repetition_sentences(
            message, draft.decision
        ):
            counts[sentence.lower()] += 1
        for question in critic_layer.company_ask_sentences(
            message, draft.decision
        ):
            normalized_company, normalized_question = critic_layer.company_ask_key(
                facts.name,
                question,
            )
            if normalized_company and normalized_question:
                company_ask_counts[(normalized_company, normalized_question)] += 1
        banned.extend(
            critic_layer.batch_repetition_sentences(message, draft.decision)
        )
        for beat in used_proof_beats(message, proof_beats or []):
            proof_counts[(draft.decision.ask, beat.beat_id)] += 1

    return drafts


def summarize(drafts: list[ReplyDraft]) -> dict[str, object]:
    messages = [d for d in drafts if d.message]
    words = [len(d.message.split()) for d in messages]
    sentence_counts: Counter[str] = Counter()
    for draft in messages:
        for sentence in critic_layer.batch_repetition_sentences(
            draft.message, draft.decision
        ):
            sentence_counts[sentence.lower()] += 1
    return {
        "total": len(drafts),
        "with_message": len(messages),
        "suppressed": sum(1 for d in drafts if d.decision.action is Action.SUPPRESS),
        "held": sum(1 for d in drafts if d.decision.action is Action.HOLD),
        "by_action": dict(Counter(d.decision.action.value for d in drafts)),
        "by_ask": dict(Counter(d.decision.ask.value for d in drafts if d.decision.ask is not Ask.NONE)),
        "would_draft_by_ask": dict(
            Counter(
                d.decision.ask.value
                for d in drafts
                if d.decision.action is Action.ASK
                and d.decision.ask is not Ask.NONE
            )
        ),
        "by_rule": dict(Counter(d.decision.rule for d in drafts)),
        "by_state": dict(Counter(d.thread_state.value for d in drafts)),
        "mean_words": round(sum(words) / len(words), 1) if words else 0,
        "max_sentence_reuse": max(sentence_counts.values(), default=0),
        "contacts_to_create": sum(len(d.decision.contacts_to_create) for d in drafts),
        "human_tasks": sum(len(d.decision.human_tasks) for d in drafts),
        "touch_cap_reached": sum(d.touch_cap_reached for d in drafts),
        "suppressed_for_2027": sum(
            1
            for d in drafts
            if d.decision.action is Action.SUPPRESS
            and "preserve for 2027 re-entry" in d.decision.reason
        ),
    }

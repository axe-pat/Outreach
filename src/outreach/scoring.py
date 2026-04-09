from __future__ import annotations

from outreach.config import ScoringWeights
from outreach.models import CandidateProfile, PriorityTier, ScoredCandidate


def score_candidate(profile: CandidateProfile, weights: ScoringWeights) -> ScoredCandidate:
    score = 0
    triggers: list[str] = []

    if profile.existing_connection:
        score += weights.existing_connection
        triggers.append("Existing Connection")

    if profile.usc_marshall:
        score += weights.usc_marshall
        triggers.append("USC Marshall")

    if profile.usc_alumni:
        score += weights.usc_alumni
        triggers.append("USC")

    if profile.shared_history:
        score += weights.shared_history
        triggers.append("Shared History")

    if profile.connection_degree == "2nd":
        score += weights.second_degree
        triggers.append("2nd Degree")
    elif profile.connection_degree == "3rd":
        score += weights.third_degree_penalty

    if profile.indian_background:
        score += weights.indian_background
        triggers.append("Indian")

    if profile.university_recruiter or profile.role_bucket == "University Recruiting":
        score += weights.university_recruiter
        triggers.append("University Recruiting")
    elif profile.role_bucket == "Product":
        score += weights.product_role
        triggers.append("Role Match")
    elif profile.role_bucket == "Engineering":
        score += weights.engineering_role
        triggers.append("Role Match")
    elif profile.role_bucket == "Adjacent":
        score += weights.adjacent_role
        triggers.append("Adjacent Role")
    elif profile.role_bucket == "Recruiting":
        score += weights.recruiter_penalty
        triggers.append("Recruiting")
    elif profile.role_bucket == "Other":
        score += weights.unrelated_role_penalty

    if profile.mutual_connections > 3:
        score += weights.strong_mutuals

    if score >= 80:
        tier = PriorityTier.HIGH
    elif score >= 35:
        tier = PriorityTier.MEDIUM
    else:
        tier = PriorityTier.LOW

    return ScoredCandidate(profile=profile, score=score, tier=tier, triggers=triggers)

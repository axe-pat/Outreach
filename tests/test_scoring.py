from outreach.config import ScoringWeights
from outreach.models import CandidateProfile, PriorityTier
from outreach.scoring import score_candidate


def test_high_priority_candidate_scores_correctly() -> None:
    candidate = CandidateProfile(
        name="Jane Doe",
        title="Senior Product Manager",
        company="Snowflake",
        linkedin_url="https://www.linkedin.com/in/jane-doe/",
        connection_degree="2nd",
        mutual_connections=5,
        usc_alumni=True,
        indian_background=False,
        role_bucket="Product",
    )

    scored = score_candidate(candidate, ScoringWeights())

    assert scored.score == 98
    assert scored.tier == PriorityTier.HIGH

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field, HttpUrl


class PriorityTier(str, Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class CandidateProfile(BaseModel):
    name: str
    title: str
    company: str
    linkedin_url: HttpUrl
    connection_degree: str
    mutual_connections: int = 0
    existing_connection: bool = False
    usc_marshall: bool = False
    usc_alumni: bool = False
    shared_history: bool = False
    indian_background: bool = False
    university_recruiter: bool = False
    role_bucket: str = "Other"


class RawSearchCandidate(BaseModel):
    name: str
    title: str | None = None
    subtitle: str | None = None
    connection_degree: str | None = None
    location: str | None = None
    linkedin_url: str | None = None
    snippet: str | None = None
    raw_text: str | None = None


class ScoredCandidate(BaseModel):
    profile: CandidateProfile
    score: int
    tier: PriorityTier
    triggers: list[str] = Field(default_factory=list)
    note: str | None = None
    run_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LinkedInCompanyQueueItem(BaseModel):
    organization_id: str
    company: str
    company_mode: str = "default"
    priority_score: int
    target_lists: str = ""
    organization_type: str = ""
    city: str = ""
    website: str = ""
    source_kind: str = ""
    status: str = ""
    team_size: int | None = None
    opportunity_count: int = 0
    contact_count: int = 0
    linkedin_contact_count: int = 0
    touchpoint_count: int = 0
    latest_opportunity_titles: list[str] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)

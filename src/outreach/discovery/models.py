from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from outreach.tracking import OpportunityType, OrganizationType, SourceKind


class DiscoveryAdapterName(str, Enum):
    YC_COMPANY_DIRECTORY = "yc_company_directory"
    BUILTIN_COMPANIES = "builtin_companies"


class DiscoverySourceDefinition(BaseModel):
    source_id: str
    label: str
    adapter: DiscoveryAdapterName
    source_kind: SourceKind = SourceKind.STARTUP_DIRECTORY
    seed_urls: list[str]
    target_lists: str = ""
    organization_type: OrganizationType = OrganizationType.STARTUP
    opportunity_type: OpportunityType | None = OpportunityType.INTERNSHIP
    discovery_notes: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)


class DiscoverySourceRegistryEntry(BaseModel):
    definition: DiscoverySourceDefinition
    summary: str
    rationale: str


class DiscoveredContact(BaseModel):
    full_name: str
    title: str = ""
    linkedin_url: str = ""
    bio: str = ""
    contact_type: str = "founder"


class DiscoveredOpportunity(BaseModel):
    title: str
    location: str = ""
    compensation_hint: str = ""
    equity_hint: str = ""
    experience_hint: str = ""
    apply_url: str = ""
    status: str = "Discovered"
    opportunity_type: OpportunityType = OpportunityType.INTERNSHIP


class DiscoveredOrganization(BaseModel):
    organization_name: str
    organization_type: OrganizationType = OrganizationType.STARTUP
    target_lists: str = ""
    city: str = ""
    website: str = ""
    company_url: str = ""
    jobs_url: str = ""
    description: str = ""
    status: str = "Discovered"
    source_kind: SourceKind = SourceKind.STARTUP_DIRECTORY
    source_page_url: str = ""
    source_item_url: str = ""
    tags: list[str] = Field(default_factory=list)
    batch: str = ""
    team_size: str = ""
    founded_year: str = ""
    location: str = ""
    jobs_count: int = 0
    opportunity_title: str = ""
    opportunity_type: OpportunityType | None = None
    contacts: list[DiscoveredContact] = Field(default_factory=list)
    opportunities: list[DiscoveredOpportunity] = Field(default_factory=list)


class DiscoveryRunResult(BaseModel):
    source: DiscoverySourceDefinition
    items: list[DiscoveredOrganization]

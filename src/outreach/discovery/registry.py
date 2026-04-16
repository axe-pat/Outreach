from __future__ import annotations

from outreach.discovery.models import (
    DiscoveryAdapterName,
    DiscoverySourceDefinition,
    DiscoverySourceRegistryEntry,
)
from outreach.tracking import OpportunityType, OrganizationType, SourceKind


SOURCE_REGISTRY: list[DiscoverySourceRegistryEntry] = [
    DiscoverySourceRegistryEntry(
        definition=DiscoverySourceDefinition(
            source_id="yc_sf_bay_hiring",
            label="YC SF Bay Area hiring startups",
            adapter=DiscoveryAdapterName.YC_COMPANY_DIRECTORY,
            source_kind=SourceKind.YC_DIRECTORY,
            seed_urls=["https://www.ycombinator.com/companies/location/san-francisco-bay-area/hiring"],
            target_lists="yc;startup;sf;hiring",
            organization_type=OrganizationType.STARTUP,
            opportunity_type=OpportunityType.OTHER,
            discovery_notes="Official YC public page filtered to Bay Area companies currently hiring.",
        ),
        summary="Official public YC directory page for Bay Area startups that are actively hiring.",
        rationale="High signal because it combines startup quality, geography, and live hiring intent.",
    ),
    DiscoverySourceRegistryEntry(
        definition=DiscoverySourceDefinition(
            source_id="yc_los_angeles",
            label="YC Los Angeles startups",
            adapter=DiscoveryAdapterName.YC_COMPANY_DIRECTORY,
            source_kind=SourceKind.YC_DIRECTORY,
            seed_urls=["https://www.ycombinator.com/companies/location/los-angeles"],
            target_lists="yc;startup;la",
            organization_type=OrganizationType.STARTUP,
            opportunity_type=OpportunityType.OTHER,
            discovery_notes="Official YC public page filtered to Los Angeles startups.",
        ),
        summary="Official public YC directory page for Los Angeles startups.",
        rationale="Good for the LA summer search even when a startup is not obviously hiring yet.",
    ),
    DiscoverySourceRegistryEntry(
        definition=DiscoverySourceDefinition(
            source_id="builtin_la_companies",
            label="Built In LA companies",
            adapter=DiscoveryAdapterName.BUILTIN_COMPANIES,
            source_kind=SourceKind.STARTUP_DIRECTORY,
            seed_urls=["https://www.builtinla.com/companies"],
            target_lists="built_in;la;companies",
            organization_type=OrganizationType.COMPANY,
            opportunity_type=OpportunityType.OTHER,
            discovery_notes="Built In Los Angeles company index with company cards and hiring links.",
        ),
        summary="Public Built In Los Angeles company index.",
        rationale="Broadens beyond YC with a larger local-company surface area and current hiring visibility.",
    ),
    DiscoverySourceRegistryEntry(
        definition=DiscoverySourceDefinition(
            source_id="builtin_sf_companies",
            label="Built In SF companies",
            adapter=DiscoveryAdapterName.BUILTIN_COMPANIES,
            source_kind=SourceKind.STARTUP_DIRECTORY,
            seed_urls=["https://www.builtinsf.com/companies"],
            target_lists="built_in;sf;companies",
            organization_type=OrganizationType.COMPANY,
            opportunity_type=OpportunityType.OTHER,
            discovery_notes="Built In San Francisco company index with company cards and hiring links.",
        ),
        summary="Public Built In San Francisco company index.",
        rationale="Gives us broad Bay Area startup and tech-company discovery beyond YC's portfolio.",
    ),
    DiscoverySourceRegistryEntry(
        definition=DiscoverySourceDefinition(
            source_id="yc_jobs",
            label="YC Work at a Startup",
            adapter=DiscoveryAdapterName.YC_COMPANY_DIRECTORY,
            source_kind=SourceKind.YC_DIRECTORY,
            seed_urls=["https://www.ycombinator.com/jobs"],
            target_lists="yc;startup;jobs",
            organization_type=OrganizationType.STARTUP,
            opportunity_type=OpportunityType.OTHER,
            discovery_notes="Official YC jobs hub, broader and noisier than the location pages.",
        ),
        summary="Official YC startup jobs hub.",
        rationale="Best follow-on source once we want more volume beyond geography-first discovery.",
    ),
]


def list_source_definitions() -> list[DiscoverySourceRegistryEntry]:
    return SOURCE_REGISTRY


def get_source_definition(source_id: str) -> DiscoverySourceRegistryEntry:
    for entry in SOURCE_REGISTRY:
        if entry.definition.source_id == source_id:
            return entry
    available = ", ".join(entry.definition.source_id for entry in SOURCE_REGISTRY)
    raise KeyError(f"Unknown discovery source '{source_id}'. Available: {available}")

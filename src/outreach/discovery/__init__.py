"""Discovery source registry and adapters for non-job outreach targets."""

from outreach.discovery.adapters import (
    BuiltInCompaniesAdapter,
    SourceAdapter,
    YCombinatorCompanyDirectoryAdapter,
)
from outreach.discovery.models import (
    DiscoveredOrganization,
    DiscoveryRunResult,
    DiscoverySourceDefinition,
    DiscoverySourceRegistryEntry,
)
from outreach.discovery.registry import get_source_definition, list_source_definitions

__all__ = [
    "DiscoveredOrganization",
    "DiscoveryRunResult",
    "DiscoverySourceDefinition",
    "DiscoverySourceRegistryEntry",
    "BuiltInCompaniesAdapter",
    "SourceAdapter",
    "YCombinatorCompanyDirectoryAdapter",
    "get_source_definition",
    "list_source_definitions",
]

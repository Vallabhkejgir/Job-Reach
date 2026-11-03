"""Lead enrichment package with dual free-tier API failover (Apollo.io + Hunter.io)."""

from src.enrichment.apollo_client import (
    ApolloClient,
    EnrichmentAPIError,
    QuotaExhaustedError,
    RateLimitError,
)
from src.enrichment.hunter_client import HunterClient
from src.enrichment.pattern_fallback import PatternFallbackEngine, infer_company_domain
from src.enrichment.router import EnrichmentFailoverRouter

__all__ = [
    "ApolloClient",
    "HunterClient",
    "PatternFallbackEngine",
    "EnrichmentFailoverRouter",
    "EnrichmentAPIError",
    "QuotaExhaustedError",
    "RateLimitError",
    "infer_company_domain",
]

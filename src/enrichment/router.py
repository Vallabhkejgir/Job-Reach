"""EnrichmentFailoverRouter: Dual Free-Tier API Failover (Apollo.io <-> Hunter.io) + Pattern/MX Fallback."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from src.enrichment.apollo_client import (
    ApolloClient,
    EnrichmentAPIError,
    QuotaExhaustedError,
    RateLimitError,
)
from src.enrichment.hunter_client import HunterClient
from src.enrichment.pattern_fallback import (
    PatternFallbackEngine,
    infer_company_domain,
)
from src.models import DecisionMaker, EnrichmentResult, JobPosting
from src.storage import StorageManager

logger = logging.getLogger(__name__)


class EnrichmentFailoverRouter:
    """Orchestrates lead enrichment across SQLite cache, Apollo.io, Hunter.io, and DNS MX fallback.

    Key capabilities:
    1. Zero-credit SQLite cache lookup first.
    2. Configurable primary/secondary provider priority (`["apollo", "hunter"]` or `["hunter", "apollo"]`).
    3. Automatic mid-run failover when the primary provider runs out of free-tier credits
       (HTTP 429, HTTP 401/402/403, or SQLite `api_quota_ledger` remaining == 0).
    4. Cross-provider handoff: if Apollo discovers a decision maker's Name + Company Domain
       but cannot unlock the email due to free-tier credit limits, the router passes that
       Name + Domain directly to Hunter's `/v2/email-finder` endpoint before trying `/v2/domain-search`.
    5. Pattern + DNS MX fallback when both APIs are exhausted or unavailable.
    """

    def __init__(
        self,
        storage: StorageManager,
        apollo_client: Optional[ApolloClient] = None,
        hunter_client: Optional[HunterClient] = None,
        pattern_engine: Optional[PatternFallbackEngine] = None,
        provider_priority: Optional[List[str]] = None,
        enable_cross_provider_handoff: bool = True,
        enable_pattern_mx_fallback: bool = True,
    ) -> None:
        self.storage = storage
        self.apollo = apollo_client or ApolloClient()
        self.hunter = hunter_client or HunterClient()
        self.pattern_engine = pattern_engine or PatternFallbackEngine()
        self.provider_priority = [
            p.lower().strip() for p in (provider_priority or ["apollo", "hunter"])
        ]
        self.enable_cross_provider_handoff = enable_cross_provider_handoff
        self.enable_pattern_mx_fallback = enable_pattern_mx_fallback
        self._last_partial_dm: Optional[DecisionMaker] = None

    @staticmethod
    def _split_name(full_name: Optional[str]) -> Tuple[str, str]:
        if not full_name:
            return "", ""
        parts = [p for p in full_name.strip().split() if p]
        if not parts:
            return "", ""
        if len(parts) == 1:
            return parts[0], ""
        return parts[0], parts[-1]

    def enrich_job(self, job: JobPosting) -> EnrichmentResult:
        """Resolves a decision-maker email for a JobPosting using dual-API failover."""
        domain = infer_company_domain(job.company, job.company_domain)
        job.company_domain = domain

        # ------------------------------------------------------------------
        # Tier 0: SQLite Zero-Credit Cache Check
        # ------------------------------------------------------------------
        cached = self.storage.get_cached_contact(
            company=job.company, company_domain=domain
        )
        if cached and cached.email:
            return EnrichmentResult(
                company=job.company,
                company_domain=domain,
                decision_maker=cached,
                provider_used="sqlite_cache",
                fallback_chain_tried=["sqlite_cache"],
                credits_consumed={"apollo": 0, "hunter": 0},
                from_cache=True,
                cross_provider_handoff_used=False,
                status_reason="Resolved from local SQLite contact cache (0 API credits)",
            )

        chain_tried: List[str] = []
        credits_used: Dict[str, int] = {"apollo": 0, "hunter": 0}
        partial_dm: Optional[DecisionMaker] = None
        cross_handoff_used = False

        # Seed partial decision maker if the job card already listed a recruiter name
        if job.recruiter_name:
            fn, ln = self._split_name(job.recruiter_name)
            if fn:
                partial_dm = DecisionMaker(
                    full_name=job.recruiter_name.strip(),
                    first_name=fn,
                    last_name=ln,
                    title=job.recruiter_title or "Talent Acquisition",
                    company=job.company,
                    company_domain=domain,
                    email=None,
                    source_provider="job_posting",
                )

        # ------------------------------------------------------------------
        # Tier 1 & Tier 2: Dual Free-Tier API Failover Loop
        # ------------------------------------------------------------------
        for provider in self.provider_priority:
            quota = self.storage.get_provider_quota(provider)
            if not quota.is_available:
                chain_tried.append(f"{provider}:skipped_quota_exhausted")
                logger.info(
                    "Skipping %s (marked exhausted in SQLite quota ledger)", provider
                )
                continue

            if provider == "apollo":
                if not self.apollo.is_configured:
                    chain_tried.append("apollo:unconfigured")
                    continue

                chain_tried.append("apollo")
                self._last_partial_dm = partial_dm
                try:
                    dm, discovered_partial, spent = self._try_apollo(
                        job=job, domain=domain, existing_partial=partial_dm
                    )
                    credits_used["apollo"] += spent
                    if discovered_partial is not None:
                        partial_dm = discovered_partial

                    if dm and dm.email:
                        self.storage.save_contact(dm)
                        return EnrichmentResult(
                            company=job.company,
                            company_domain=domain,
                            decision_maker=dm,
                            provider_used="apollo",
                            fallback_chain_tried=chain_tried,
                            credits_consumed=credits_used,
                            from_cache=False,
                            cross_provider_handoff_used=False,
                            status_reason="Enriched via Apollo.io API",
                        )
                except (QuotaExhaustedError, RateLimitError) as exc:
                    if self._last_partial_dm is not None:
                        partial_dm = self._last_partial_dm
                    logger.warning(
                        "Apollo free-tier quota exhausted (%s). Failing over to secondary provider.",
                        exc,
                    )
                    self.storage.mark_provider_exhausted("apollo", str(exc))
                    chain_tried.append("apollo:quota_exhausted")
                except EnrichmentAPIError as exc:
                    if self._last_partial_dm is not None:
                        partial_dm = self._last_partial_dm
                    logger.warning("Apollo API error (%s). Failing over.", exc)
                    chain_tried.append("apollo:api_error")

            elif provider == "hunter":
                if not self.hunter.is_configured:
                    chain_tried.append("hunter:unconfigured")
                    continue

                chain_tried.append("hunter")
                try:
                    dm, handoff_triggered, spent = self._try_hunter(
                        job=job, domain=domain, partial_dm=partial_dm
                    )
                    credits_used["hunter"] += spent
                    cross_handoff_used = cross_handoff_used or handoff_triggered

                    if dm and dm.email:
                        self.storage.save_contact(dm)
                        reason = (
                            "Enriched via Cross-Provider Handoff (Apollo Name+Domain -> Hunter Email Finder)"
                            if handoff_triggered
                            else "Enriched via Hunter.io API"
                        )
                        return EnrichmentResult(
                            company=job.company,
                            company_domain=domain,
                            decision_maker=dm,
                            provider_used="hunter",
                            fallback_chain_tried=chain_tried,
                            credits_consumed=credits_used,
                            from_cache=False,
                            cross_provider_handoff_used=handoff_triggered,
                            status_reason=reason,
                        )
                except (QuotaExhaustedError, RateLimitError) as exc:
                    logger.warning(
                        "Hunter.io free-tier quota exhausted (%s). Failing over.",
                        exc,
                    )
                    self.storage.mark_provider_exhausted("hunter", str(exc))
                    chain_tried.append("hunter:quota_exhausted")
                except EnrichmentAPIError as exc:
                    logger.warning("Hunter.io API error (%s). Failing over.", exc)
                    chain_tried.append("hunter:api_error")

        # ------------------------------------------------------------------
        # Tier 3: Pattern Permutation + DNS MX Verification Fallback
        # ------------------------------------------------------------------
        if self.enable_pattern_mx_fallback:
            chain_tried.append("pattern_mx_fallback")
            cached_pattern_row = self.storage.get_company_pattern(domain)
            known_pattern = (
                cached_pattern_row["email_pattern"]
                if cached_pattern_row
                else self.hunter.last_discovered_pattern
            )
            person_name = (
                partial_dm.full_name
                if partial_dm
                else job.recruiter_name
            )
            person_title = (
                partial_dm.title
                if partial_dm
                else (job.recruiter_title or "Talent Acquisition Team")
            )

            fallback_dm = self.pattern_engine.build_fallback_contact(
                company=job.company,
                company_domain=domain,
                full_name=person_name,
                title=person_title,
                known_pattern=known_pattern,
            )
            if fallback_dm and fallback_dm.email:
                self.storage.save_contact(fallback_dm)
                return EnrichmentResult(
                    company=job.company,
                    company_domain=domain,
                    decision_maker=fallback_dm,
                    provider_used="pattern_mx_fallback",
                    fallback_chain_tried=chain_tried,
                    credits_consumed=credits_used,
                    from_cache=False,
                    cross_provider_handoff_used=cross_handoff_used,
                    status_reason="Resolved via Pattern Permutation + DNS MX Verification Fallback",
                )

        return EnrichmentResult(
            company=job.company,
            company_domain=domain,
            decision_maker=None,
            provider_used="none",
            fallback_chain_tried=chain_tried,
            credits_consumed=credits_used,
            from_cache=False,
            cross_provider_handoff_used=cross_handoff_used,
            status_reason="All enrichment tiers exhausted or unable to resolve a valid email",
        )

    def _try_apollo(
        self,
        job: JobPosting,
        domain: str,
        existing_partial: Optional[DecisionMaker],
    ) -> Tuple[Optional[DecisionMaker], Optional[DecisionMaker], int]:
        """Executes Apollo search and optional email unlock. Returns (unlocked_dm, partial_dm, credits_spent)."""
        spent = 0
        partial_dm = existing_partial

        # If we already have a recruiter's name from the job card, try unlocking directly first
        if partial_dm and partial_dm.first_name and partial_dm.last_name:
            unlocked = self.apollo.unlock_person_email(
                first_name=partial_dm.first_name,
                last_name=partial_dm.last_name,
                company_domain=domain,
                company_name=job.company,
                title=partial_dm.title,
            )
            spent += 1
            self._sync_or_decrement("apollo", self.apollo.last_remaining_credits, 1)
            if unlocked and unlocked.email:
                return unlocked, partial_dm, spent

        # Search decision makers at the target company domain
        people = self.apollo.search_decision_makers(
            company_domain=domain, company_name=job.company
        )
        spent += 1
        self._sync_or_decrement("apollo", self.apollo.last_remaining_credits, 1)

        if not people:
            return None, partial_dm, spent

        best_person = people[0]
        if best_person.email:
            return best_person, best_person, spent

        # Apollo found the person's Name + Domain, but the email is still locked!
        # Save as partial_dm so even if unlock raises QuotaExhaustedError, Hunter can use it.
        partial_dm = best_person
        self._last_partial_dm = best_person

        quota = self.storage.get_provider_quota("apollo")
        if not quota.is_available:
            raise QuotaExhaustedError(
                "apollo",
                "Apollo search succeeded (Name+Domain found) but 0 credits remain to unlock email",
            )

        unlocked = self.apollo.unlock_person_email(
            first_name=best_person.first_name,
            last_name=best_person.last_name,
            company_domain=best_person.company_domain or domain,
            company_name=job.company,
            title=best_person.title,
            linkedin_url=best_person.linkedin_url,
        )
        spent += 1
        self._sync_or_decrement("apollo", self.apollo.last_remaining_credits, 1)
        return unlocked, partial_dm, spent

    def _try_hunter(
        self,
        job: JobPosting,
        domain: str,
        partial_dm: Optional[DecisionMaker],
    ) -> Tuple[Optional[DecisionMaker], bool, int]:
        """Executes Hunter email-finder (if Name+Domain known) or domain-search."""
        spent = 0
        handoff_used = False

        # Cross-provider handoff: if Apollo (or job card) provided Name + Domain, call /v2/email-finder
        if (
            self.enable_cross_provider_handoff
            and partial_dm
            and partial_dm.first_name
            and partial_dm.last_name
        ):
            handoff_used = partial_dm.source_provider == "apollo"
            found = self.hunter.find_email(
                company_domain=partial_dm.company_domain or domain,
                first_name=partial_dm.first_name,
                last_name=partial_dm.last_name,
                company_name=job.company,
                title=partial_dm.title,
                linkedin_url=partial_dm.linkedin_url,
            )
            spent += 1
            self._sync_or_decrement("hunter", self.hunter.last_remaining_credits, 1)
            if found and found.email:
                if handoff_used:
                    found.source_provider = "apollo+hunter_handoff"
                return found, handoff_used, spent

        # Check if Hunter still has quota before falling back to /v2/domain-search
        quota = self.storage.get_provider_quota("hunter")
        if not quota.is_available:
            raise QuotaExhaustedError(
                "hunter", "Hunter credits exhausted after email-finder attempt"
            )

        contacts, pattern = self.hunter.domain_search(
            company_domain=domain, company_name=job.company
        )
        spent += 1
        self._sync_or_decrement("hunter", self.hunter.last_remaining_credits, 1)

        if pattern:
            self.storage.save_company_pattern(
                company_domain=domain,
                company=job.company,
                email_pattern=pattern,
                mx_valid=True,
            )

        if contacts:
            return contacts[0], False, spent

        return None, handoff_used, spent

    def _sync_or_decrement(
        self,
        provider: str,
        header_remaining: Optional[int],
        default_decrement: int = 1,
    ) -> None:
        if header_remaining is not None:
            self.storage.sync_provider_quota(provider, header_remaining)
        else:
            self.storage.decrement_provider_quota(provider, default_decrement)

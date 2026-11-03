#!/usr/bin/env python3
"""Job-Reach CLI Orchestrator: Entry-level job scouting, dual-API enrichment, and cold outreach."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from src.enrichment.apollo_client import ApolloClient
from src.enrichment.hunter_client import HunterClient
from src.enrichment.pattern_fallback import PatternFallbackEngine
from src.enrichment.router import EnrichmentFailoverRouter
from src.models import JobPosting, OutreachDraft
from src.outreach.composer import OutreachComposer
from src.outreach.dispatcher import OutreachDispatcher
from src.scrapers.exp_filter import ExperienceFilter
from src.scrapers.linkedin_scraper import LinkedInScraper
from src.scrapers.naukri_scraper import NaukriScraper
from src.storage import StorageManager


def load_dotenv_file(env_path: str | Path = ".env") -> None:
    """Loads environment variables from `.env` using python-dotenv or built-in parser."""
    path = Path(env_path)
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=path)
    except ImportError:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, val = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip("'\""))


def load_config(config_path: str | Path = "config.yaml") -> Dict[str, Any]:
    path = Path(config_path)
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def run_demo_pipeline(dry_run: bool = True) -> int:
    """Executes a complete offline demonstration of all Job-Reach stages."""
    print("=" * 78)
    print(" JOB-REACH AUTOMATION PIPELINE — END-TO-END DEMO (DRY-RUN)")
    print("=" * 78)

    storage = StorageManager(
        db_path=":memory:", apollo_monthly_limit=2, hunter_monthly_limit=2
    )
    exp_filter = ExperienceFilter(allow_edge_cases=True)

    # ------------------------------------------------------------------
    # Stage 1: Job Scraping & Experience/Seniority Filtering
    # ------------------------------------------------------------------
    print("\n[STAGE 1] Evaluating Scraped Listings (Naukri.com & LinkedIn)...")
    sample_listings = [
        JobPosting(
            job_id="naukri_101",
            platform="naukri",
            title="AI Engineer (LLM & RAG)",
            company="Sarvam AI Labs",
            company_domain="sarvam.ai",
            location="Bangalore",
            experience_raw="0 - 2 yrs",
            skills=["Python", "PyTorch", "LangChain", "RAG", "FastAPI"],
            job_url="https://www.naukri.com/job-listings-ai-engineer-101",
        ),
        JobPosting(
            job_id="linkedin_102",
            platform="linkedin",
            title="Associate Data Scientist",
            company="Fractal Analytics",
            company_domain="fractal.ai",
            location="Mumbai",
            experience_raw="1 - 3 years",
            seniority_badge="Entry level",
            skills=["Python", "Scikit-Learn", "SQL", "XGBoost"],
            job_url="https://www.linkedin.com/jobs/view/102",
        ),
        JobPosting(
            job_id="naukri_103",
            platform="naukri",
            title="Software Developer (SDE-I)",
            company="Razorpay",
            company_domain="razorpay.com",
            location="Bangalore",
            experience_raw="Fresher / 0 - 1 yrs",
            skills=["Python", "Go", "REST APIs", "PostgreSQL"],
            job_url="https://www.naukri.com/job-listings-sde-1-103",
        ),
        JobPosting(
            job_id="linkedin_104",
            platform="linkedin",
            title="Backend Engineer",
            company="Zepto",
            company_domain="zeptonow.com",
            location="Bangalore",
            experience_raw="1 - 2 years",
            seniority_badge="Entry level",
            skills=["Python", "FastAPI", "Redis", "PostgreSQL", "Docker"],
            job_url="https://www.linkedin.com/jobs/view/104",
        ),
        # Rejected listings (Senior titles or >3 years experience)
        JobPosting(
            job_id="naukri_901",
            platform="naukri",
            title="Senior AI Engineer",
            company="BigCorp",
            location="Bangalore",
            experience_raw="0 - 2 yrs",  # Even with 0-2 yrs badge, "Senior" title must reject!
        ),
        JobPosting(
            job_id="linkedin_902",
            platform="linkedin",
            title="Lead Backend Developer",
            company="ScaleUp Inc",
            location="Hyderabad",
            experience_raw="2 - 3 years",
        ),
        JobPosting(
            job_id="naukri_903",
            platform="naukri",
            title="Software Engineer",
            company="EnterpriseTech",
            location="Pune",
            experience_raw="3 - 6 yrs",
        ),
    ]

    accepted_jobs: List[JobPosting] = []
    for job in sample_listings:
        verdict = exp_filter.apply_to_job(job)
        badge = (
            f"ACCEPTED [{verdict.category.value}]"
            if verdict.is_accepted
            else "REJECTED [EXCLUDED]"
        )
        print(
            f"  - {badge:<22} | {job.platform:<8} | {job.title:<28} "
            f"| Exp: {job.experience_raw:<18} | {verdict.reason}"
        )
        if verdict.is_accepted:
            storage.upsert_job(job)
            accepted_jobs.append(job)

    # ------------------------------------------------------------------
    # Stage 2: Dual Free-Tier API Failover (Apollo <-> Hunter + Handoff + MX)
    # ------------------------------------------------------------------
    print("\n[STAGE 2] Lead Enrichment with Dual Free-Tier API Failover...")

    def mock_apollo_transport(
        method: str, url: str, payload: Dict[str, Any], headers: Dict[str, str]
    ) -> Tuple[int, Dict[str, str], Dict[str, Any]]:
        domain = payload.get("q_organization_domains") or payload.get("domain")
        if domain == "sarvam.ai":
            # Job 1: Apollo primary succeeds with unlocked email (1 credit left)
            return (
                200,
                {"x-rate-limit-remaining": "1"},
                {
                    "people": [
                        {
                            "first_name": "Ananya",
                            "last_name": "Sharma",
                            "name": "Ananya Sharma",
                            "title": "Head of Talent Acquisition",
                            "email": "ananya.sharma@sarvam.ai",
                            "email_status": "verified",
                            "organization": {"primary_domain": "sarvam.ai"},
                        }
                    ]
                },
            )
        if domain == "fractal.ai" and "/mixed_people/search" in url:
            # Job 2: Apollo finds person Name + Domain, but email is locked and credits hit 0!
            return (
                200,
                {"x-rate-limit-remaining": "0"},
                {
                    "people": [
                        {
                            "first_name": "Rohan",
                            "last_name": "Mehta",
                            "name": "Rohan Mehta",
                            "title": "Lead Technical Recruiter",
                            "email": "email_not_unlocked@domain.com",
                            "organization": {"primary_domain": "fractal.ai"},
                        }
                    ]
                },
            )
        # Subsequent Apollo calls return HTTP 429 free-tier quota exhausted
        return (
            429,
            {"x-rate-limit-remaining": "0"},
            {"error": "Free tier monthly credit limit reached"},
        )

    def mock_hunter_transport(
        method: str, url: str, params: Dict[str, Any], headers: Dict[str, str]
    ) -> Tuple[int, Dict[str, str], Dict[str, Any]]:
        domain = params.get("domain")
        if "/email-finder" in url and domain == "fractal.ai":
            # Job 2: Cross-Provider Handoff! Apollo's Name+Domain resolved via Hunter /email-finder
            return (
                200,
                {"x-ratelimit-remaining": "1"},
                {
                    "data": {
                        "first_name": params.get("first_name"),
                        "last_name": params.get("last_name"),
                        "email": "rohan.mehta@fractal.ai",
                        "score": 94,
                        "position": "Lead Technical Recruiter",
                        "verification": {"status": "valid"},
                    }
                },
            )
        if "/domain-search" in url and domain == "razorpay.com":
            # Job 3: Apollo already marked exhausted -> Router goes straight to Hunter /domain-search
            return (
                200,
                {"x-ratelimit-remaining": "0"},
                {
                    "data": {
                        "organization": "Razorpay",
                        "pattern": "{first}.{last}",
                        "emails": [
                            {
                                "value": "priya.nair@razorpay.com",
                                "first_name": "Priya",
                                "last_name": "Nair",
                                "position": "Engineering Recruitment Partner",
                                "confidence": 91,
                                "verification": {"status": "valid"},
                            }
                        ],
                    }
                },
            )
        return (
            429,
            {"x-ratelimit-remaining": "0"},
            {"errors": [{"id": "too_many_requests", "details": "Hunter free quota exhausted"}]},
        )

    apollo_client = ApolloClient(
        api_key="demo_apollo_key", http_transport=mock_apollo_transport
    )
    hunter_client = HunterClient(
        api_key="demo_hunter_key", http_transport=mock_hunter_transport
    )
    pattern_engine = PatternFallbackEngine(mx_Lookup_fn=lambda d: True)
    router = EnrichmentFailoverRouter(
        storage=storage,
        apollo_client=apollo_client,
        hunter_client=hunter_client,
        pattern_engine=pattern_engine,
        provider_priority=["apollo", "hunter"],
    )

    enriched_pairs = []
    for job in accepted_jobs:
        res = router.enrich_job(job)
        dm = res.decision_maker
        print(
            f"  - {job.company:<18} | Provider: {res.provider_used:<20} "
            f"| Contact: {(dm.full_name if dm else 'N/A'):<22} "
            f"| Email: {(dm.email if dm else 'N/A'):<28} "
            f"| Chain: {' -> '.join(res.fallback_chain_tried)}"
        )
        if dm and dm.email:
            enriched_pairs.append((job, dm))

    # ------------------------------------------------------------------
    # Stage 3: Role-Tailored Outreach Composition & Dry-Run Dispatch
    # ------------------------------------------------------------------
    print("\n[STAGE 3] Composing Role-Specific Cold Emails & Running Dispatcher...")
    config = load_config("config.yaml")
    composer = OutreachComposer(candidate_profile=config.get("candidate"))
    dispatcher = OutreachDispatcher(
        storage=storage,
        transport="smtp",
        dry_run=dry_run,
        daily_send_cap=25,
    )

    drafts: List[OutreachDraft] = [
        composer.compose(job, dm) for job, dm in enriched_pairs
    ]
    outcomes = dispatcher.dispatch_batch(drafts)

    for draft, outcome in zip(drafts, outcomes):
        print(
            f"  - [{outcome.status}] To: {draft.recipient_email:<28} "
            f"| Role: {draft.role_category:<18} | Subject: {draft.subject}"
        )

    if drafts:
        sample = drafts[0]
        print("\n" + "-" * 78)
        print(f" SAMPLE GENERATED EMAIL ({sample.role_title} @ {sample.company})")
        print("-" * 78)
        print(f"To:      {sample.recipient_name} <{sample.recipient_email}>")
        print(f"Subject: {sample.subject}\n")
        print(sample.body_text)
        print("-" * 78)

    apollo_q = storage.get_provider_quota("apollo")
    hunter_q = storage.get_provider_quota("hunter")
    print(
        f"\n[QUOTA LEDGER] Apollo remaining={apollo_q.credits_remaining}/{apollo_q.monthly_limit} "
        f"(exhausted={apollo_q.exhausted}) | Hunter remaining={hunter_q.credits_remaining}/{hunter_q.monthly_limit} "
        f"(exhausted={hunter_q.exhausted})"
    )
    print("=" * 78)
    return 0


async def run_live_scrape(
    config: Dict[str, Any], storage: StorageManager
) -> List[JobPosting]:
    """Runs live Playwright scraping across configured platforms and roles."""
    scraping_cfg = config.get("scraping") or {}
    platforms = scraping_cfg.get("platforms") or ["naukri", "linkedin"]
    locations = scraping_cfg.get("locations") or ["Bangalore"]
    max_pages = int(scraping_cfg.get("max_pages_per_role", 1))
    headless = bool(scraping_cfg.get("headless", True))
    target_roles = scraping_cfg.get("target_roles") or []

    exp_filter = ExperienceFilter(
        allow_edge_cases=bool(
            (config.get("filtering") or {}).get("allow_edge_cases", True)
        )
    )
    discovered: List[JobPosting] = []

    if "naukri" in platforms:
        async with NaukriScraper(
            exp_filter=exp_filter, headless=headless, max_pages=max_pages
        ) as naukri:
            for role_entry in target_roles:
                kw = (role_entry.get("keywords") or [role_entry.get("display_name")])[0]
                for loc in locations[:2]:
                    jobs = await naukri.scrape_role(kw, loc, max_pages=max_pages)
                    for j in jobs:
                        storage.upsert_job(j)
                        discovered.append(j)

    if "linkedin" in platforms:
        async with LinkedInScraper(
            exp_filter=exp_filter, headless=headless, max_pages=max_pages
        ) as linkedin:
            for role_entry in target_roles:
                kw = (role_entry.get("keywords") or [role_entry.get("display_name")])[0]
                for loc in locations[:2]:
                    jobs = await linkedin.scrape_role(kw, loc, max_pages=max_pages)
                    for j in jobs:
                        storage.upsert_job(j)
                        discovered.append(j)

    return discovered


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-reach",
        description="Automated Entry-Level Job Scouting, Dual-API Lead Enrichment & Cold Outreach Pipeline.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to YAML configuration file (default: config.yaml)",
    )
    parser.add_argument(
        "--db",
        default=os.getenv("JOB_REACH_DB_PATH", "job_reach_state.db"),
        help="Path to SQLite state database",
    )
    parser.add_argument(
        "--scrape",
        action="store_true",
        help="Run Playwright scrapers on Naukri.com and LinkedIn for 0-2 yr roles",
    )
    parser.add_argument(
        "--enrich",
        action="store_true",
        help="Enrich scraped jobs using Apollo.io <-> Hunter.io dual failover router",
    )
    parser.add_argument(
        "--draft",
        action="store_true",
        help="Compose personalized cold outreach drafts for enriched contacts",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Dispatch outreach emails via configured transport (SMTP TLS or Gmail OAuth2)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Force dry-run mode (log and preview emails without live transmission)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run a self-contained offline demonstration of scraping, failover enrichment, and outreach",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    load_dotenv_file()
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.demo:
        return run_demo_pipeline(dry_run=True)

    config = load_config(args.config)
    enrichment_cfg = config.get("enrichment") or {}
    outreach_cfg = config.get("outreach") or {}

    storage = StorageManager(
        db_path=args.db,
        apollo_monthly_limit=int(
            enrichment_cfg.get("apollo_monthly_free_limit", 50)
        ),
        hunter_monthly_limit=int(
            enrichment_cfg.get("hunter_monthly_free_limit", 25)
        ),
    )

    if not any([args.scrape, args.enrich, args.draft, args.send]):
        parser.print_help()
        return 0

    if args.scrape:
        asyncio.run(run_live_scrape(config, storage))

    priority = list(
        enrichment_cfg.get("provider_priority") or ["apollo", "hunter"]
    )
    env_primary = os.getenv("ENRICHMENT_PRIMARY_PROVIDER", "").strip().lower()
    if env_primary in ("apollo", "hunter"):
        secondary = "hunter" if env_primary == "apollo" else "apollo"
        priority = [env_primary, secondary]

    router = EnrichmentFailoverRouter(
        storage=storage,
        apollo_client=ApolloClient(api_key=os.getenv("APOLLO_API_KEY")),
        hunter_client=HunterClient(api_key=os.getenv("HUNTER_API_KEY")),
        pattern_engine=PatternFallbackEngine(),
        provider_priority=priority,
        enable_cross_provider_handoff=bool(
            enrichment_cfg.get("enable_cross_provider_handoff", True)
        ),
        enable_pattern_mx_fallback=bool(
            enrichment_cfg.get("enable_pattern_mx_fallback", True)
        ),
    )

    jobs = storage.get_jobs(limit=100)
    enriched_pairs = []

    if args.enrich or args.draft or args.send:
        for job in jobs:
            res = router.enrich_job(job)
            if res.decision_maker and res.decision_maker.email:
                enriched_pairs.append((job, res.decision_maker))

    if args.draft or args.send:
        composer = OutreachComposer(
            candidate_profile=config.get("candidate"),
            enable_llm_hook=bool(outreach_cfg.get("enable_llm_hook", False)),
        )
        drafts = [composer.compose(job, dm) for job, dm in enriched_pairs]

        is_dry_run = args.dry_run or (not args.send)
        dispatcher = OutreachDispatcher(
            storage=storage,
            transport=os.getenv(
                "MAIL_TRANSPORT", outreach_cfg.get("transport", "smtp")
            ),
            dry_run=is_dry_run,
            daily_send_cap=int(outreach_cfg.get("daily_send_cap", 25)),
            dedup_window_days=int(outreach_cfg.get("dedup_window_days", 30)),
            min_jitter_seconds=float(
                outreach_cfg.get("min_jitter_seconds", 45.0)
            ),
            max_jitter_seconds=float(
                outreach_cfg.get("max_jitter_seconds", 120.0)
            ),
            sender_name=(config.get("candidate") or {}).get(
                "name", "Alex Rivera"
            ),
            sender_email=os.getenv(
                "CANDIDATE_EMAIL",
                (config.get("candidate") or {}).get("email"),
            ),
            resume_path=os.getenv(
                "CANDIDATE_RESUME_PATH",
                (config.get("candidate") or {}).get("resume_path"),
            ),
        )
        outcomes = dispatcher.dispatch_batch(drafts)
        for outcome in outcomes:
            logging.info(
                "Dispatch outcome for %s (%s): %s - %s",
                outcome.recipient_email,
                outcome.job_id,
                outcome.status,
                outcome.reason,
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())

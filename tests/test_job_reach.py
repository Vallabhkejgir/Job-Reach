"""Comprehensive unit tests for Job-Reach experience filtering, dual-API failover, and outreach."""

from __future__ import annotations

from typing import Any, Dict, Tuple

import pytest

from src.enrichment.apollo_client import ApolloClient
from src.enrichment.hunter_client import HunterClient
from src.enrichment.pattern_fallback import (
    PatternFallbackEngine,
    infer_company_domain,
)
from src.enrichment.router import EnrichmentFailoverRouter
from src.models import DecisionMaker, ExperienceCategory, JobPosting, OutreachDraft
from src.outreach.composer import OutreachComposer
from src.outreach.dispatcher import OutreachDispatcher
from src.scrapers.exp_filter import (
    ExperienceFilter,
    classify_experience,
    infer_role_category,
)
from src.scrapers.linkedin_scraper import LinkedInScraper
from src.scrapers.naukri_scraper import NaukriScraper
from src.storage import StorageManager


# ============================================================================
# 1. Experience & Seniority Gatekeeper Tests
# ============================================================================


@pytest.mark.parametrize(
    "title,exp_text,expected_min,expected_max",
    [
        ("AI Engineer", "0 - 2 yrs", 0.0, 2.0),
        ("Software Developer", "0-1 years", 0.0, 1.0),
        ("Backend Engineer", "1 - 2 yrs", 1.0, 2.0),
        ("Junior Data Scientist", "Fresher", 0.0, 0.0),
        ("Associate Software Engineer", "Up to 2 years", 0.0, 2.0),
        ("Backend Engineer", "2+ years", 2.0, 2.0),
    ],
)
def test_primary_entry_level_experience_accepted(
    title: str, exp_text: str, expected_min: float, expected_max: float
) -> None:
    verdict = classify_experience(title=title, experience_text=exp_text)
    assert verdict.is_accepted is True
    assert verdict.category == ExperienceCategory.PRIMARY
    assert verdict.is_edge_case is False
    assert verdict.excluded_by_seniority is False
    assert verdict.min_years == expected_min
    assert verdict.max_years == expected_max


@pytest.mark.parametrize(
    "title,exp_text,expected_min,expected_max",
    [
        ("Data Scientist", "1 - 3 yrs", 1.0, 3.0),
        ("AI Engineer", "0 - 3 years", 0.0, 3.0),
        ("Backend Engineer", "2 - 3 years", 2.0, 3.0),
        ("Software Developer", "Up to 3 years", 0.0, 3.0),
    ],
)
def test_edge_case_1_to_3_years_accepted_and_flagged(
    title: str, exp_text: str, expected_min: float, expected_max: float
) -> None:
    verdict = classify_experience(title=title, experience_text=exp_text)
    assert verdict.is_accepted is True
    assert verdict.category == ExperienceCategory.EDGE_CASE
    assert verdict.is_edge_case is True
    assert verdict.min_years == expected_min
    assert verdict.max_years == expected_max


@pytest.mark.parametrize(
    "title,exp_text",
    [
        ("Software Developer", "2 - 4 yrs"),
        ("AI Engineer", "3 - 5 years"),
        ("Data Scientist", "3 - 6 yrs"),
        ("Backend Engineer", "3+ years"),
        ("Software Engineer", "Minimum 4 years of experience"),
        ("Data Scientist", "5 years of experience"),
        ("Backend Developer", "4 - 8 yrs"),
    ],
)
def test_over_3_years_experience_rejected(title: str, exp_text: str) -> None:
    verdict = classify_experience(title=title, experience_text=exp_text)
    assert verdict.is_accepted is False
    assert verdict.category == ExperienceCategory.EXCLUDED


@pytest.mark.parametrize(
    "senior_title,exp_text,seniority_badge",
    [
        ("Senior AI Engineer", "0 - 2 yrs", ""),
        ("Sr. Data Scientist", "0 - 1 yrs", ""),
        ("Sr Software Developer", "Fresher", ""),
        ("Lead Backend Engineer", "1 - 2 years", ""),
        ("Principal AI Engineer", "0 - 2 years", ""),
        ("Staff Software Engineer", "", ""),
        ("Engineering Manager", "0 - 2 yrs", ""),
        ("Director of Data Science", "", ""),
        ("VP of Engineering", "", ""),
        ("Solutions Architect", "1 - 2 yrs", ""),
        ("Software Engineer (SDE-III)", "0 - 2 yrs", ""),
        ("Backend Developer - SDE 3", "1 - 3 yrs", ""),
        ("AI Engineer", "0 - 2 yrs", "Mid-Senior level"),
        ("Data Scientist", "1 - 2 yrs", "Director"),
        ("Backend Engineer", "0 - 2 yrs", "Executive"),
    ],
)
def test_senior_titles_and_badges_strictly_rejected(
    senior_title: str, exp_text: str, seniority_badge: str
) -> None:
    """Senior/Lead/Principal/Manager titles must be rejected even if experience says 0-2 yrs."""
    verdict = classify_experience(
        title=senior_title,
        experience_text=exp_text,
        seniority_badge=seniority_badge,
    )
    assert verdict.is_accepted is False
    assert verdict.category == ExperienceCategory.EXCLUDED
    assert verdict.excluded_by_seniority is True


def test_role_category_inference() -> None:
    assert infer_role_category("GenAI & LLM Engineer") == "ai_engineer"
    assert infer_role_category("Associate Data Scientist") == "data_scientist"
    assert infer_role_category("Python Backend Engineer") == "backend_engineer"
    assert infer_role_category("Software Developer (SDE-I)") == "software_developer"


# ============================================================================
# 2. Scraper HTML Parsing & Filtering Tests
# ============================================================================


def test_naukri_html_parser_filters_out_senior_and_3plus_jobs() -> None:
    sample_html = """
    <div class="srp-jobtuple-wrapper" data-job-id="111">
        <a class="title" href="https://naukri.com/jd/111">AI Engineer</a>
        <a class="comp-name">Acme AI</a>
        <span class="expwdth">0-2 Yrs</span>
        <span class="locWdth">Bangalore</span>
        <span class="job-desc">Build RAG and PyTorch models.</span>
    </div>
    <div class="srp-jobtuple-wrapper" data-job-id="222">
        <a class="title" href="https://naukri.com/jd/222">Senior Backend Engineer</a>
        <a class="comp-name">Acme AI</a>
        <span class="expwdth">0-2 Yrs</span>
        <span class="locWdth">Bangalore</span>
    </div>
    <div class="srp-jobtuple-wrapper" data-job-id="333">
        <a class="title" href="https://naukri.com/jd/333">Data Scientist</a>
        <a class="comp-name">DataWorks</a>
        <span class="expwdth">1-3 Yrs</span>
        <span class="locWdth">Mumbai</span>
    </div>
    <div class="srp-jobtuple-wrapper" data-job-id="444">
        <a class="title" href="https://naukri.com/jd/444">Software Developer</a>
        <a class="comp-name">LegacyCorp</a>
        <span class="expwdth">4-7 Yrs</span>
        <span class="locWdth">Pune</span>
    </div>
    """
    scraper = NaukriScraper()
    jobs = scraper.parse_html_cards(sample_html)
    assert len(jobs) == 2
    assert jobs[0].title == "AI Engineer"
    assert jobs[0].experience_category == ExperienceCategory.PRIMARY
    assert jobs[0].is_edge_case is False

    assert jobs[1].title == "Data Scientist"
    assert jobs[1].experience_category == ExperienceCategory.EDGE_CASE
    assert jobs[1].is_edge_case is True


def test_linkedin_html_parser_respects_seniority_badges() -> None:
    sample_html = """
    <div class="base-card" data-entity-urn="urn:li:jobPosting:501">
        <h3 class="base-search-card__title">Backend Engineer</h3>
        <h4 class="base-search-card__subtitle">CloudScale</h4>
        <span class="job-search-card__location">Hyderabad</span>
        <div class="show-more-less-html__markup">Requires 0-2 years of Python & FastAPI experience.</div>
        <span class="description__job-criteria-text">Entry level</span>
    </div>
    <div class="base-card" data-entity-urn="urn:li:jobPosting:502">
        <h3 class="base-search-card__title">Backend Engineer</h3>
        <h4 class="base-search-card__subtitle">CloudScale</h4>
        <span class="job-search-card__location">Hyderabad</span>
        <div class="show-more-less-html__markup">Requires 1-2 years experience.</div>
        <span class="description__job-criteria-text">Mid-Senior level</span>
    </div>
    """
    scraper = LinkedInScraper()
    jobs = scraper.parse_html_cards(sample_html)
    assert len(jobs) == 1
    assert jobs[0].title == "Backend Engineer"
    assert jobs[0].experience_category == ExperienceCategory.PRIMARY


# ============================================================================
# 3. Dual Free-Tier API Failover Router Tests (Apollo <-> Hunter + MX)
# ============================================================================


def test_router_sqlite_cache_zero_credit_hit() -> None:
    storage = StorageManager(db_path=":memory:")
    cached_dm = DecisionMaker(
        full_name="Meera Krishnan",
        first_name="Meera",
        last_name="Krishnan",
        title="Technical Recruiter",
        company="CacheCorp",
        company_domain="cachecorp.com",
        email="meera@cachecorp.com",
        source_provider="apollo",
        confidence_score=95,
        verified=True,
    )
    storage.save_contact(cached_dm)

    router = EnrichmentFailoverRouter(storage=storage)
    job = JobPosting(
        job_id="j1",
        platform="naukri",
        title="AI Engineer",
        company="CacheCorp",
        company_domain="cachecorp.com",
        location="Bangalore",
    )
    res = router.enrich_job(job)
    assert res.succeeded is True
    assert res.from_cache is True
    assert res.provider_used == "sqlite_cache"
    assert res.decision_maker is not None
    assert res.decision_maker.email == "meera@cachecorp.com"
    assert res.credits_consumed == {"apollo": 0, "hunter": 0}


def test_router_apollo_primary_to_hunter_failover_on_quota_exhaustion() -> None:
    """When Apollo hits HTTP 429 quota exhaustion, router automatically fails over to Hunter.io."""
    storage = StorageManager(
        db_path=":memory:", apollo_monthly_limit=5, hunter_monthly_limit=5
    )

    def apollo_exhausted_transport(
        method: str, url: str, payload: Dict[str, Any], headers: Dict[str, str]
    ) -> Tuple[int, Dict[str, str], Dict[str, Any]]:
        return (
            429,
            {"x-rate-limit-remaining": "0"},
            {"error": "Apollo monthly free-tier credit limit exceeded"},
        )

    def hunter_success_transport(
        method: str, url: str, params: Dict[str, Any], headers: Dict[str, str]
    ) -> Tuple[int, Dict[str, str], Dict[str, Any]]:
        return (
            200,
            {"x-ratelimit-remaining": "4"},
            {
                "data": {
                    "organization": "FailoverLabs",
                    "pattern": "{first}.{last}",
                    "emails": [
                        {
                            "value": "vikram.patel@failoverlabs.com",
                            "first_name": "Vikram",
                            "last_name": "Patel",
                            "position": "Engineering Manager",
                            "confidence": 93,
                            "verification": {"status": "valid"},
                        }
                    ],
                }
            },
        )

    apollo = ApolloClient(
        api_key="live_apollo_key", http_transport=apollo_exhausted_transport
    )
    hunter = HunterClient(
        api_key="live_hunter_key", http_transport=hunter_success_transport
    )
    router = EnrichmentFailoverRouter(
        storage=storage,
        apollo_client=apollo,
        hunter_client=hunter,
        provider_priority=["apollo", "hunter"],
    )

    job = JobPosting(
        job_id="j2",
        platform="linkedin",
        title="Backend Engineer",
        company="FailoverLabs",
        company_domain="failoverlabs.com",
        location="Bangalore",
    )
    res = router.enrich_job(job)

    assert res.succeeded is True
    assert res.provider_used == "hunter"
    assert res.decision_maker is not None
    assert res.decision_maker.email == "vikram.patel@failoverlabs.com"
    assert "apollo:quota_exhausted" in res.fallback_chain_tried
    assert "hunter" in res.fallback_chain_tried

    # Verify SQLite quota ledger marked Apollo as exhausted
    apollo_quota = storage.get_provider_quota("apollo")
    assert apollo_quota.exhausted is True
    assert apollo_quota.credits_remaining == 0


def test_router_cross_provider_name_and_domain_handoff() -> None:
    """If Apollo finds Name+Domain but cannot unlock email due to 0 credits, Hunter email-finder completes it."""
    storage = StorageManager(
        db_path=":memory:", apollo_monthly_limit=1, hunter_monthly_limit=5
    )

    def apollo_partial_transport(
        method: str, url: str, payload: Dict[str, Any], headers: Dict[str, str]
    ) -> Tuple[int, Dict[str, str], Dict[str, Any]]:
        # Search succeeds with Name + Domain, but email is locked and credits drop to 0
        return (
            200,
            {"x-rate-limit-remaining": "0"},
            {
                "people": [
                    {
                        "first_name": "Neha",
                        "last_name": "Kapoor",
                        "name": "Neha Kapoor",
                        "title": "Talent Acquisition Lead",
                        "email": "email_not_unlocked@domain.com",
                        "organization": {"primary_domain": "neuralsys.ai"},
                    }
                ]
            },
        )

    def hunter_finder_transport(
        method: str, url: str, params: Dict[str, Any], headers: Dict[str, str]
    ) -> Tuple[int, Dict[str, str], Dict[str, Any]]:
        assert "/email-finder" in url
        assert params.get("first_name") == "Neha"
        assert params.get("last_name") == "Kapoor"
        assert params.get("domain") == "neuralsys.ai"
        return (
            200,
            {"x-ratelimit-remaining": "4"},
            {
                "data": {
                    "first_name": "Neha",
                    "last_name": "Kapoor",
                    "email": "neha.kapoor@neuralsys.ai",
                    "score": 96,
                    "position": "Talent Acquisition Lead",
                    "verification": {"status": "valid"},
                }
            },
        )

    router = EnrichmentFailoverRouter(
        storage=storage,
        apollo_client=ApolloClient(
            api_key="apollo_key", http_transport=apollo_partial_transport
        ),
        hunter_client=HunterClient(
            api_key="hunter_key", http_transport=hunter_finder_transport
        ),
        provider_priority=["apollo", "hunter"],
    )

    job = JobPosting(
        job_id="j3",
        platform="naukri",
        title="AI Engineer",
        company="NeuralSys AI",
        company_domain="neuralsys.ai",
        location="Bangalore",
    )
    res = router.enrich_job(job)

    assert res.succeeded is True
    assert res.provider_used == "hunter"
    assert res.cross_provider_handoff_used is True
    assert res.decision_maker is not None
    assert res.decision_maker.email == "neha.kapoor@neuralsys.ai"
    assert res.decision_maker.source_provider == "apollo+hunter_handoff"


def test_router_reverse_priority_hunter_to_apollo_failover() -> None:
    """When configured with ['hunter', 'apollo'], Hunter quota exhaustion fails over to Apollo."""
    storage = StorageManager(
        db_path=":memory:", apollo_monthly_limit=5, hunter_monthly_limit=5
    )

    def hunter_exhausted(
        method: str, url: str, params: Dict[str, Any], headers: Dict[str, str]
    ) -> Tuple[int, Dict[str, str], Dict[str, Any]]:
        return (
            429,
            {"x-ratelimit-remaining": "0"},
            {"errors": [{"id": "too_many_requests", "details": "Quota reached"}]},
        )

    def apollo_success(
        method: str, url: str, payload: Dict[str, Any], headers: Dict[str, str]
    ) -> Tuple[int, Dict[str, str], Dict[str, Any]]:
        return (
            200,
            {"x-rate-limit-remaining": "4"},
            {
                "people": [
                    {
                        "first_name": "Arjun",
                        "last_name": "Rao",
                        "name": "Arjun Rao",
                        "title": "VP of Talent",
                        "email": "arjun.rao@quantloop.io",
                        "email_status": "verified",
                        "organization": {"primary_domain": "quantloop.io"},
                    }
                ]
            },
        )

    router = EnrichmentFailoverRouter(
        storage=storage,
        apollo_client=ApolloClient(
            api_key="apollo_key", http_transport=apollo_success
        ),
        hunter_client=HunterClient(
            api_key="hunter_key", http_transport=hunter_exhausted
        ),
        provider_priority=["hunter", "apollo"],
    )

    job = JobPosting(
        job_id="j4",
        platform="linkedin",
        title="Data Scientist",
        company="QuantLoop",
        company_domain="quantloop.io",
        location="Mumbai",
    )
    res = router.enrich_job(job)

    assert res.succeeded is True
    assert res.provider_used == "apollo"
    assert res.decision_maker is not None
    assert res.decision_maker.email == "arjun.rao@quantloop.io"
    assert storage.get_provider_quota("hunter").exhausted is True


def test_router_pattern_mx_fallback_when_both_apis_exhausted() -> None:
    """When both Apollo and Hunter are exhausted in SQLite, router resolves via Pattern + DNS MX."""
    storage = StorageManager(db_path=":memory:")
    storage.mark_provider_exhausted("apollo", "0 credits left")
    storage.mark_provider_exhausted("hunter", "0 credits left")
    storage.save_company_pattern(
        company_domain="skyforge.dev",
        company="SkyForge",
        email_pattern="{first}.{last}",
        mx_valid=True,
    )

    pattern_engine = PatternFallbackEngine(mx_Lookup_fn=lambda domain: True)
    router = EnrichmentFailoverRouter(
        storage=storage,
        pattern_engine=pattern_engine,
        provider_priority=["apollo", "hunter"],
    )

    job = JobPosting(
        job_id="j5",
        platform="naukri",
        title="Software Developer",
        company="SkyForge",
        company_domain="skyforge.dev",
        location="Pune",
        recruiter_name="Kiran Desai",
        recruiter_title="HR Specialist",
    )
    res = router.enrich_job(job)

    assert res.succeeded is True
    assert res.provider_used == "pattern_mx_fallback"
    assert res.decision_maker is not None
    assert res.decision_maker.email == "kiran.desai@skyforge.dev"
    assert "apollo:skipped_quota_exhausted" in res.fallback_chain_tried
    assert "hunter:skipped_quota_exhausted" in res.fallback_chain_tried


def test_infer_company_domain() -> None:
    assert infer_company_domain("Acme Technologies Pvt. Ltd.") == "acme.com"
    assert infer_company_domain("Razorpay", "https://www.razorpay.com/careers") == "razorpay.com"


# ============================================================================
# 4. Outreach Composer & Dispatcher Tests
# ============================================================================


def test_composer_tailors_by_role_and_edge_case() -> None:
    composer = OutreachComposer(
        candidate_profile={
            "name": "Alex Rivera",
            "email": "alex@example.com",
            "portfolio_url": "https://github.com/alexrivera",
            "linkedin_url": "https://linkedin.com/in/alexrivera",
        }
    )
    dm = DecisionMaker(
        full_name="Ananya Sharma",
        first_name="Ananya",
        last_name="Sharma",
        title="Technical Recruiter",
        company="Sarvam AI",
        company_domain="sarvam.ai",
        email="ananya@sarvam.ai",
    )
    edge_job = JobPosting(
        job_id="job_edge_1",
        platform="linkedin",
        title="AI Engineer",
        company="Sarvam AI",
        location="Bangalore",
        experience_raw="1 - 3 years",
        experience_category=ExperienceCategory.EDGE_CASE,
        is_edge_case=True,
        role_category="ai_engineer",
        skills=["Python", "PyTorch", "RAG"],
    )

    draft = composer.compose(edge_job, dm)
    assert "AI Engineer" in draft.subject
    assert "Sarvam AI" in draft.subject
    assert "Hi Ananya," in draft.body_text
    assert "Python, PyTorch, RAG" in draft.body_text
    assert "1 - 3 years" in draft.body_text
    assert "contribute from day one" in draft.body_text
    assert draft.body_html is not None and "<p>" in draft.body_html


def test_dispatcher_dry_run_dedup_and_daily_cap() -> None:
    storage = StorageManager(db_path=":memory:")
    draft = OutreachDraft(
        job_id="job_1",
        recipient_email="recruiter@targetco.com",
        recipient_name="Priya Nair",
        recipient_title="Recruiter",
        company="TargetCo",
        role_title="Backend Engineer",
        role_category="backend_engineer",
        subject="Backend Engineer Application - Alex Rivera",
        body_text="Hello Priya, I would love to apply.",
    )

    # 1. Dry-run logs DRY_RUN without blocking future live send
    dry_dispatcher = OutreachDispatcher(
        storage=storage, transport="smtp", dry_run=True, daily_send_cap=2
    )
    outcome_dry = dry_dispatcher.dispatch_single(draft)
    assert outcome_dry.status == "DRY_RUN"
    assert storage.was_contacted_recently("recruiter@targetco.com") is False

    # 2. Simulate a SENT record -> subsequent dispatch within 30 days is blocked as duplicate
    storage.log_outreach(draft, transport="smtp", status="SENT")
    assert storage.was_contacted_recently("recruiter@targetco.com") is True

    outcome_dup = dry_dispatcher.dispatch_single(draft)
    assert outcome_dup.status == "SKIPPED_DUPLICATE"

    # 3. Simulate reaching daily send cap (2/2) -> next new recipient is blocked by cap
    second_draft = OutreachDraft(
        job_id="job_2",
        recipient_email="second@otherco.com",
        recipient_name="Rohan",
        recipient_title="Hiring Manager",
        company="OtherCo",
        role_title="Data Scientist",
        role_category="data_scientist",
        subject="Data Scientist Application",
        body_text="Hi Rohan",
    )
    storage.log_outreach(second_draft, transport="smtp", status="SENT")
    assert storage.get_daily_send_count() == 2

    third_draft = OutreachDraft(
        job_id="job_3",
        recipient_email="third@thirdco.com",
        recipient_name="Meera",
        recipient_title="CTO",
        company="ThirdCo",
        role_title="Software Developer",
        role_category="software_developer",
        subject="Software Developer Application",
        body_text="Hi Meera",
    )
    outcome_cap = dry_dispatcher.dispatch_single(third_draft)
    assert outcome_cap.status == "SKIPPED_DAILY_CAP"

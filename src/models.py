"""Core domain data models for the Job-Reach automation pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class ExperienceCategory(str, Enum):
    """Classification buckets for job experience requirements."""

    PRIMARY = "PRIMARY"        # 0-2 years (strictly entry-level / fresher)
    EDGE_CASE = "EDGE_CASE"    # 1-3 or 0-3 years (valid stretch target)
    UNLISTED = "UNLISTED"      # No explicit years, passed entry-level title check
    EXCLUDED = "EXCLUDED"      # >3 years or explicitly senior/lead/manager


@dataclass
class FilterVerdict:
    """Result of evaluating a job posting against experience and seniority rules."""

    is_accepted: bool
    category: ExperienceCategory
    min_years: Optional[float] = None
    max_years: Optional[float] = None
    is_edge_case: bool = False
    excluded_by_seniority: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["category"] = self.category.value
        return data


@dataclass
class JobPosting:
    """Normalized representation of a scraped job listing from Naukri or LinkedIn."""

    job_id: str
    platform: str
    title: str
    company: str
    location: str
    experience_raw: str = ""
    min_years: Optional[float] = None
    max_years: Optional[float] = None
    experience_category: ExperienceCategory = ExperienceCategory.UNLISTED
    is_edge_case: bool = False
    role_category: str = "software_developer"
    job_url: str = ""
    company_domain: Optional[str] = None
    recruiter_name: Optional[str] = None
    recruiter_title: Optional[str] = None
    description_snippet: str = ""
    seniority_badge: str = ""
    skills: List[str] = field(default_factory=list)
    posted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["experience_category"] = (
            self.experience_category.value
            if isinstance(self.experience_category, ExperienceCategory)
            else str(self.experience_category)
        )
        return data


@dataclass
class DecisionMaker:
    """Identified hiring manager, recruiter, or technical lead for a target company."""

    full_name: str
    first_name: str
    last_name: str
    title: str
    company: str
    company_domain: str
    email: Optional[str] = None
    linkedin_url: Optional[str] = None
    source_provider: str = "unknown"
    confidence_score: int = 0
    verified: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EnrichmentResult:
    """Outcome of the multi-provider lead enrichment failover pipeline."""

    company: str
    company_domain: str
    decision_maker: Optional[DecisionMaker] = None
    provider_used: str = "none"
    fallback_chain_tried: List[str] = field(default_factory=list)
    credits_consumed: Dict[str, int] = field(default_factory=dict)
    from_cache: bool = False
    cross_provider_handoff_used: bool = False
    status_reason: str = ""

    @property
    def succeeded(self) -> bool:
        return bool(self.decision_maker and self.decision_maker.email)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["succeeded"] = self.succeeded
        return data


@dataclass
class OutreachDraft:
    """Personalized cold outreach email ready for dispatch or dry-run inspection."""

    job_id: str
    recipient_email: str
    recipient_name: str
    recipient_title: str
    company: str
    role_title: str
    role_category: str
    subject: str
    body_text: str
    body_html: Optional[str] = None
    is_edge_case: bool = False
    template_used: str = "jinja2_default"
    job_url: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderQuotaStatus:
    """Tracks free-tier API credit availability and exhaustion state in SQLite."""

    provider: str
    credits_remaining: int
    monthly_limit: int
    exhausted: bool = False
    reset_at: Optional[str] = None
    last_error: Optional[str] = None
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def is_available(self) -> bool:
        return (not self.exhausted) and self.credits_remaining > 0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["is_available"] = self.is_available
        return data

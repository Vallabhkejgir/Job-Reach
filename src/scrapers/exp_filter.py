"""Strict Experience & Seniority Gatekeeper for Entry-Level Job Scouting.

Rules enforced:
1. Immediate Senior Exclusion:
   Any posting whose title or seniority badge contains Senior, Sr., Sr, Lead,
   Principal, Staff, Architect, Manager, Director, Head, VP, Chief, SDE-III,
   SDE 3, or LinkedIn 'Mid-Senior level / Director / Executive' is rejected
   immediately (EXCLUDED), regardless of listed experience years.
2. Experience Range Classification:
   - PRIMARY:   min_years <= 2 and max_years <= 2 (e.g., 0-1, 0-2, 1-2 yrs, Fresher)
   - EDGE_CASE: min_years <= 2 and max_years == 3 (e.g., 1-3 yrs, 0-3 yrs, 2-3 yrs)
   - EXCLUDED:  min_years >= 3 or max_years > 3 (e.g., 2-4 yrs, 3-5 yrs, 3+ yrs, 5+ yrs)
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

from src.models import ExperienceCategory, FilterVerdict, JobPosting

# Word-boundary regex matching senior, managerial, or staff-plus titles
SENIOR_TITLE_PATTERN = re.compile(
    r"""
    (?:^|[\s\(\[\-/,\.|])
    (
        senior
        | sr\.?
        | lead
        | principal
        | staff
        | architect
        | manager
        | director
        | head(?:\s+of)?
        | vp
        | vice\s+president
        | chief
        | founding\s+senior
        | sde[\s\-]*iii
        | sde[\s\-]*3
        | swe[\s\-]*iii
        | swe[\s\-]*3
        | l[45678]\b
    )
    (?:$|[\s\)\]\-/,\.:;])
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)

EXCLUDED_SENIORITY_BADGES = {
    "mid-senior level",
    "mid-senior",
    "senior level",
    "director",
    "executive",
    "lead",
    "manager",
}

FRESHER_PATTERN = re.compile(
    r"(?i)\b(fresher|freshers|entry[\s\-]*level|new\s+grad|graduate\s+trainee|no\s+prior\s+experience|0\s+years?)\b"
)

# e.g., "0 - 2 yrs", "1-3 years", "0 to 3 yr", "2 - 5 Years of experience"
RANGE_EXP_PATTERN = re.compile(
    r"(?i)\b(\d+(?:\.\d+)?)\s*(?:[-–—]|to)\s*(\d+(?:\.\d+)?)\s*\+?\s*(?:yrs?|years?)\b"
)

# e.g., "up to 2 years", "upto 3 yrs", "max 2 years"
UP_TO_EXP_PATTERN = re.compile(
    r"(?i)\b(?:up\s*to|maximum|max)\s+(\d+(?:\.\d+)?)\s*(?:yrs?|years?)\b"
)

# e.g., "3+ years", "minimum 4 years", "at least 3 yrs", "min 2 yrs", "4+ yrs"
PLUS_OR_MIN_EXP_PATTERN = re.compile(
    r"(?i)(?:\b(?:min(?:imum)?|at\s+least|over|more\s+than)\s+(\d+(?:\.\d+)?)\s*\+?\s*(?:yrs?|years?)\b|\b(\d+(?:\.\d+)?)\s*\+\s*(?:yrs?|years?)\b)"
)

# e.g., "require 4 years of experience", "5 years experience"
EXACT_EXP_PATTERN = re.compile(
    r"(?i)\b(\d+(?:\.\d+)?)\s*(?:yrs?|years?)\s+(?:of\s+)?(?:relevant\s+|work\s+|industry\s+|professional\s+)?experience\b"
)


def has_excluded_seniority(
    title: str, seniority_badge: str = ""
) -> Tuple[bool, Optional[str]]:
    """Checks if a job title or seniority badge indicates a non-entry-level role."""
    clean_title = (title or "").strip()
    match = SENIOR_TITLE_PATTERN.search(clean_title)
    if match:
        token = match.group(1).strip()
        return True, f"Title contains excluded senior keyword '{token}'"

    clean_badge = (seniority_badge or "").strip().lower()
    if clean_badge:
        for badge_tok in EXCLUDED_SENIORITY_BADGES:
            if badge_tok in clean_badge:
                return (
                    True,
                    f"Seniority badge '{seniority_badge.strip()}' is excluded",
                )

    return False, None


def parse_experience_bounds(
    experience_text: str, description_text: str = ""
) -> Tuple[Optional[float], Optional[float]]:
    """Extracts (min_years, max_years) from structured badge text or JD body."""
    for source in (experience_text or "", description_text or ""):
        text = source.strip()
        if not text:
            continue

        # 1. Explicit range: "0-2 yrs", "1-3 years", "2 to 4 years"
        range_matches = list(RANGE_EXP_PATTERN.finditer(text))
        if range_matches:
            pairs = [
                (float(m.group(1)), float(m.group(2))) for m in range_matches
            ]
            # Use the primary range (or lowest min if multiple mentioned)
            pairs.sort(key=lambda p: (p[0], p[1]))
            return pairs[0]

        # 2. "Up to X years"
        up_to_match = UP_TO_EXP_PATTERN.search(text)
        if up_to_match:
            return 0.0, float(up_to_match.group(1))

        # 3. "X+ years" or "minimum X years"
        plus_matches = list(PLUS_OR_MIN_EXP_PATTERN.finditer(text))
        if plus_matches:
            vals = [
                float(m.group(1) or m.group(2))
                for m in plus_matches
                if (m.group(1) or m.group(2))
            ]
            if vals:
                min_v = min(vals)
                return min_v, min_v

        # 4. Exact "X years of experience"
        exact_match = EXACT_EXP_PATTERN.search(text)
        if exact_match:
            val = float(exact_match.group(1))
            return val, val

        # 5. Explicit fresher / entry-level token
        if FRESHER_PATTERN.search(text):
            return 0.0, 0.0

    return None, None


def classify_experience(
    title: str,
    experience_text: str = "",
    description_text: str = "",
    seniority_badge: str = "",
    allow_edge_cases: bool = True,
    allow_unlisted_if_entry_level: bool = True,
) -> FilterVerdict:
    """Evaluates a job posting against strict seniority and 0-2 / 1-3 year bounds."""
    # Gate 1: Immediate exclusion of Senior/Lead/Principal/Manager/Director roles
    is_senior, senior_reason = has_excluded_seniority(title, seniority_badge)
    if is_senior:
        min_y, max_y = parse_experience_bounds(experience_text, description_text)
        return FilterVerdict(
            is_accepted=False,
            category=ExperienceCategory.EXCLUDED,
            min_years=min_y,
            max_years=max_y,
            is_edge_case=False,
            excluded_by_seniority=True,
            reason=senior_reason or "Excluded senior role",
        )

    # Gate 2: Parse numeric experience bounds
    min_y, max_y = parse_experience_bounds(experience_text, description_text)

    if min_y is None or max_y is None:
        if allow_unlisted_if_entry_level:
            return FilterVerdict(
                is_accepted=True,
                category=ExperienceCategory.UNLISTED,
                min_years=None,
                max_years=None,
                is_edge_case=False,
                excluded_by_seniority=False,
                reason="No explicit experience years; title passed entry-level check",
            )
        return FilterVerdict(
            is_accepted=False,
            category=ExperienceCategory.EXCLUDED,
            min_years=None,
            max_years=None,
            is_edge_case=False,
            excluded_by_seniority=False,
            reason="Experience unlisted and unlisted roles are disabled",
        )

    # Gate 3: Reject any requirement with min_years >= 3 or max_years > 3
    if min_y >= 3.0 or max_y > 3.0:
        return FilterVerdict(
            is_accepted=False,
            category=ExperienceCategory.EXCLUDED,
            min_years=min_y,
            max_years=max_y,
            is_edge_case=False,
            excluded_by_seniority=False,
            reason=f"Requires {min_y:g}-{max_y:g} years (exceeds entry-level limit of <=2 primary / <=3 edge case)",
        )

    # Gate 4: Strictly 0-2 years -> PRIMARY
    if min_y <= 2.0 and max_y <= 2.0:
        return FilterVerdict(
            is_accepted=True,
            category=ExperienceCategory.PRIMARY,
            min_years=min_y,
            max_years=max_y,
            is_edge_case=False,
            excluded_by_seniority=False,
            reason=f"Matches primary entry-level target ({min_y:g}-{max_y:g} years)",
        )

    # Gate 5: 1-3 years / 0-3 years / 2-3 years (min_y <= 2 and 2 < max_y <= 3) -> EDGE_CASE
    if min_y <= 2.0 and 2.0 < max_y <= 3.0:
        if allow_edge_cases:
            return FilterVerdict(
                is_accepted=True,
                category=ExperienceCategory.EDGE_CASE,
                min_years=min_y,
                max_years=max_y,
                is_edge_case=True,
                excluded_by_seniority=False,
                reason=f"Matches valid edge-case range ({min_y:g}-{max_y:g} years)",
            )
        return FilterVerdict(
            is_accepted=False,
            category=ExperienceCategory.EXCLUDED,
            min_years=min_y,
            max_years=max_y,
            is_edge_case=True,
            excluded_by_seniority=False,
            reason=f"Edge case ({min_y:g}-{max_y:g} years) disabled by configuration",
        )

    return FilterVerdict(
        is_accepted=False,
        category=ExperienceCategory.EXCLUDED,
        min_years=min_y,
        max_years=max_y,
        is_edge_case=False,
        excluded_by_seniority=False,
        reason=f"Outside supported experience bounds ({min_y:g}-{max_y:g} years)",
    )


def infer_role_category(title: str) -> str:
    """Normalizes a job title into one of the four target role categories."""
    lowered = (title or "").lower()
    if any(
        kw in lowered
        for kw in (
            "ai ",
            "ai/",
            "artificial intelligence",
            "machine learning",
            "ml engineer",
            "genai",
            "llm",
            "nlp",
            "deep learning",
            "computer vision",
        )
    ) or lowered.startswith("ai"):
        return "ai_engineer"

    if any(
        kw in lowered
        for kw in (
            "data scientist",
            "data science",
            "decision scientist",
            "applied scientist",
        )
    ):
        return "data_scientist"

    if any(
        kw in lowered
        for kw in (
            "backend",
            "back-end",
            "back end",
            "api engineer",
            "distributed systems",
            "platform engineer",
        )
    ):
        return "backend_engineer"

    return "software_developer"


class ExperienceFilter:
    """Configurable filter wrapper for evaluating and annotating JobPosting objects."""

    def __init__(
        self,
        allow_edge_cases: bool = True,
        allow_unlisted_if_entry_level: bool = True,
    ) -> None:
        self.allow_edge_cases = allow_edge_cases
        self.allow_unlisted_if_entry_level = allow_unlisted_if_entry_level

    def evaluate(
        self,
        title: str,
        experience_text: str = "",
        description_text: str = "",
        seniority_badge: str = "",
    ) -> FilterVerdict:
        return classify_experience(
            title=title,
            experience_text=experience_text,
            description_text=description_text,
            seniority_badge=seniority_badge,
            allow_edge_cases=self.allow_edge_cases,
            allow_unlisted_if_entry_level=self.allow_unlisted_if_entry_level,
        )

    def apply_to_job(self, job: JobPosting) -> FilterVerdict:
        """Evaluates a JobPosting in place, updating its experience fields and role category."""
        verdict = self.evaluate(
            title=job.title,
            experience_text=job.experience_raw,
            description_snippet=job.description_snippet,
            seniority_badge=job.seniority_badge,
        )
        job.min_years = verdict.min_years
        job.max_years = verdict.max_years
        job.experience_category = verdict.category
        job.is_edge_case = verdict.is_edge_case
        if not job.role_category or job.role_category == "software_developer":
            job.role_category = infer_role_category(job.title)
        return verdict

    def evaluate(  # type: ignore[no-redef]
        self,
        title: str,
        experience_text: str = "",
        description_text: str = "",
        seniority_badge: str = "",
        description_snippet: str = "",
    ) -> FilterVerdict:
        desc = description_text or description_snippet
        return classify_experience(
            title=title,
            experience_text=experience_text,
            description_text=desc,
            seniority_badge=seniority_badge,
            allow_edge_cases=self.allow_edge_cases,
            allow_unlisted_if_entry_level=self.allow_unlisted_if_entry_level,
        )

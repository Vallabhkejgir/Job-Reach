"""Naukri.com job scraper and HTML parser for entry-level engineering roles."""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Dict, List, Optional
from urllib.parse import quote_plus

from src.models import JobPosting
from src.scrapers.base import BaseJobScraper
from src.scrapers.exp_filter import infer_role_category

logger = logging.getLogger(__name__)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "software-developer"


class NaukriScraper(BaseJobScraper):
    """Scrapes Naukri.com job listings and filters strictly for 0-2 yr (and 1-3 yr edge) roles."""

    platform_name = "naukri"

    def build_search_url(
        self, role_keyword: str, location: str, page_num: int = 1, experience: int = 0
    ) -> str:
        role_slug = _slugify(role_keyword)
        loc_slug = _slugify(location)
        base_path = f"https://www.naukri.com/{role_slug}-jobs-in-{loc_slug}"
        if page_num > 1:
            base_path = f"{base_path}-{page_num}"
        query = f"?k={quote_plus(role_keyword)}&l={quote_plus(location)}&experience={experience}"
        return f"{base_path}{query}"

    def parse_html_cards(
        self, html_content: str, role_category: Optional[str] = None
    ) -> List[JobPosting]:
        """Parses Naukri job cards from HTML and applies the strict ExperienceFilter."""
        raw_cards = self._extract_raw_cards(html_content)
        accepted_jobs: List[JobPosting] = []

        for card in raw_cards:
            title = card.get("title", "").strip()
            company = card.get("company", "").strip()
            if not title or not company:
                continue

            exp_raw = card.get("experience", "").strip()
            desc = card.get("description", "").strip()
            location = card.get("location", "India").strip()
            job_url = card.get("job_url", "").strip()
            job_id = card.get("job_id") or self._derive_job_id(title, company, job_url)
            skills = [s.strip() for s in card.get("skills", []) if s.strip()]
            recruiter_name = card.get("recruiter_name") or None

            verdict = self.exp_filter.evaluate(
                title=title,
                experience_text=exp_raw,
                description_text=desc,
            )
            if not verdict.is_accepted:
                logger.debug(
                    "Rejected Naukri listing '%s' at '%s': %s",
                    title,
                    company,
                    verdict.reason,
                )
                continue

            posting = JobPosting(
                job_id=f"naukri_{job_id}",
                platform="naukri",
                title=title,
                company=company,
                location=location,
                experience_raw=exp_raw,
                min_years=verdict.min_years,
                max_years=verdict.max_years,
                experience_category=verdict.category,
                is_edge_case=verdict.is_edge_case,
                role_category=role_category or infer_role_category(title),
                job_url=job_url,
                recruiter_name=recruiter_name,
                description_snippet=desc,
                skills=skills,
            )
            accepted_jobs.append(posting)

        return accepted_jobs

    def _extract_raw_cards(self, html_content: str) -> List[Dict[str, any]]:
        """Extracts job card fields using BeautifulSoup4 when installed or regex fallback."""
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html_content, "html.parser")
            card_nodes = soup.select(
                "div.srp-jobtuple-wrapper, article.jobTuple, div.cust-job-tuple"
            )
            cards: List[Dict[str, any]] = []
            for node in card_nodes:
                title_el = node.select_one("a.title")
                comp_el = node.select_one("a.comp-name, a.subTitle")
                exp_el = node.select_one(
                    "span.expwdth, li.experience, span.exp"
                )
                loc_el = node.select_one(
                    "span.locWdth, li.location, span.loc"
                )
                desc_el = node.select_one(
                    "span.job-desc, div.job-description"
                )
                recruiter_el = node.select_one("span.recruiter-name")
                skill_els = node.select("ul.tags-gt li, ul.tags li")

                cards.append(
                    {
                        "job_id": node.get("data-job-id"),
                        "title": title_el.get_text(strip=True) if title_el else "",
                        "job_url": title_el.get("href", "") if title_el else "",
                        "company": comp_el.get_text(strip=True) if comp_el else "",
                        "experience": exp_el.get_text(strip=True) if exp_el else "",
                        "location": loc_el.get_text(strip=True) if loc_el else "",
                        "description": desc_el.get_text(strip=True) if desc_el else "",
                        "recruiter_name": (
                            recruiter_el.get_text(strip=True)
                            if recruiter_el
                            else None
                        ),
                        "skills": [s.get_text(strip=True) for s in skill_els],
                    }
                )
            return cards
        except ImportError:
            return self._regex_parse_naukri_cards(html_content)

    @staticmethod
    def _regex_parse_naukri_cards(html_content: str) -> List[Dict[str, any]]:
        cards: List[Dict[str, any]] = []
        blocks = re.findall(
            r'<(?:div|article)[^>]*class="[^"]*(?:srp-jobtuple-wrapper|jobTuple|cust-job-tuple)[^"]*"[^>]*>(.*?)</(?:div|article)>\s*(?=<(?:div|article)|$)',
            html_content,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if not blocks:
            blocks = [html_content]
        for block in blocks:
            title_m = re.search(
                r'<a[^>]*class="[^"]*title[^"]*"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                block,
                flags=re.DOTALL | re.IGNORECASE,
            )
            if not title_m:
                continue
            comp_m = re.search(
                r'<a[^>]*class="[^"]*(?:comp-name|subTitle)[^"]*"[^>]*>(.*?)</a>',
                block,
                flags=re.DOTALL | re.IGNORECASE,
            )
            exp_m = re.search(
                r'<(?:span|li)[^>]*class="[^"]*(?:expwdth|experience|exp)[^"]*"[^>]*>(.*?)</(?:span|li)>',
                block,
                flags=re.DOTALL | re.IGNORECASE,
            )
            loc_m = re.search(
                r'<(?:span|li)[^>]*class="[^"]*(?:locWdth|location|loc)[^"]*"[^>]*>(.*?)</(?:span|li)>',
                block,
                flags=re.DOTALL | re.IGNORECASE,
            )
            desc_m = re.search(
                r'<(?:span|div)[^>]*class="[^"]*(?:job-desc|job-description)[^"]*"[^>]*>(.*?)</(?:span|div)>',
                block,
                flags=re.DOTALL | re.IGNORECASE,
            )
            cards.append(
                {
                    "job_id": None,
                    "title": re.sub(r"<[^>]+>", "", title_m.group(2)).strip(),
                    "job_url": title_m.group(1).strip(),
                    "company": (
                        re.sub(r"<[^>]+>", "", comp_m.group(1)).strip()
                        if comp_m
                        else ""
                    ),
                    "experience": (
                        re.sub(r"<[^>]+>", "", exp_m.group(1)).strip()
                        if exp_m
                        else ""
                    ),
                    "location": (
                        re.sub(r"<[^>]+>", "", loc_m.group(1)).strip()
                        if loc_m
                        else "India"
                    ),
                    "description": (
                        re.sub(r"<[^>]+>", "", desc_m.group(1)).strip()
                        if desc_m
                        else ""
                    ),
                    "recruiter_name": None,
                    "skills": [],
                }
            )
        return cards

    @staticmethod
    def _derive_job_id(title: str, company: str, job_url: str) -> str:
        raw = f"{company.lower()}::{title.lower()}::{job_url}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    async def scrape_role(
        self, role_keyword: str, location: str, max_pages: Optional[int] = None
    ) -> List[JobPosting]:
        """Scrapes Naukri.com pages using Playwright and filters results."""
        if self._context is None:
            await self.start_browser()

        pages_to_scrape = max_pages or self.max_pages
        role_cat = infer_role_category(role_keyword)
        collected: List[JobPosting] = []
        page = await self._context.new_page()

        try:
            for page_idx in range(1, pages_to_scrape + 1):
                url = self.build_search_url(
                    role_keyword=role_keyword,
                    location=location,
                    page_num=page_idx,
                    experience=0,
                )
                logger.info("Scraping Naukri URL: %s", url)
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await self.human_delay()
                await self.human_scroll(page, steps=4)
                html = await page.content()
                page_jobs = self.parse_html_cards(html, role_category=role_cat)
                collected.extend(page_jobs)
        finally:
            await page.close()

        return collected

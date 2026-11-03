"""LinkedIn job scraper and HTML parser for entry-level engineering roles."""

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


class LinkedInScraper(BaseJobScraper):
    """Scrapes LinkedIn Jobs with f_E=1,2 (Internship & Entry-Level) and filters out >3 yr roles."""

    platform_name = "linkedin"

    def build_search_url(
        self, role_keyword: str, location: str, start_offset: int = 0
    ) -> str:
        """Constructs a LinkedIn job search URL filtered to entry-level & recent listings."""
        kw = quote_plus(role_keyword)
        loc = quote_plus(location)
        # f_E=1%2C2 -> Internship (1) and Entry Level (2); f_TPR=r86400 -> Past 24 hours
        return (
            f"https://www.linkedin.com/jobs/search/?keywords={kw}"
            f"&location={loc}&f_E=1%2C2&f_TPR=r86400&start={start_offset}"
        )

    def parse_html_cards(
        self, html_content: str, role_category: Optional[str] = None
    ) -> List[JobPosting]:
        """Parses LinkedIn job cards and JD snippets, enforcing ExperienceFilter."""
        raw_cards = self._extract_raw_cards(html_content)
        accepted_jobs: List[JobPosting] = []

        for card in raw_cards:
            title = card.get("title", "").strip()
            company = card.get("company", "").strip()
            if not title or not company:
                continue

            location = card.get("location", "Remote").strip()
            exp_raw = card.get("experience", "").strip()
            desc = card.get("description", "").strip()
            seniority = card.get("seniority_badge", "").strip()
            job_url = card.get("job_url", "").strip()
            recruiter_name = card.get("recruiter_name") or None
            recruiter_title = card.get("recruiter_title") or None
            job_id = card.get("job_id") or self._derive_job_id(title, company, job_url)

            verdict = self.exp_filter.evaluate(
                title=title,
                experience_text=exp_raw,
                description_text=desc,
                seniority_badge=seniority,
            )
            if not verdict.is_accepted:
                logger.debug(
                    "Rejected LinkedIn listing '%s' at '%s': %s",
                    title,
                    company,
                    verdict.reason,
                )
                continue

            posting = JobPosting(
                job_id=f"linkedin_{job_id}",
                platform="linkedin",
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
                recruiter_title=recruiter_title,
                description_snippet=desc,
                seniority_badge=seniority,
            )
            accepted_jobs.append(posting)

        return accepted_jobs

    def _extract_raw_cards(self, html_content: str) -> List[Dict[str, any]]:
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html_content, "html.parser")
            card_nodes = soup.select(
                "div.base-card, li.jobs-search-results__list-item, div.job-search-card"
            )
            cards: List[Dict[str, any]] = []
            for node in card_nodes:
                title_el = node.select_one(
                    "h3.base-search-card__title, a.job-card-list__title"
                )
                comp_el = node.select_one(
                    "h4.base-search-card__subtitle, a.job-card-container__company-name"
                )
                loc_el = node.select_one(
                    "span.job-search-card__location, li.job-card-container__metadata-item"
                )
                link_el = node.select_one("a.base-card__full-link, a.job-card-list__title")
                desc_el = node.select_one(
                    "div.show-more-less-html__markup, p.job-description"
                )
                seniority_el = node.select_one(
                    "span.description__job-criteria-text, span.seniority-badge"
                )
                recruiter_el = node.select_one("span.hirer-card__hirer-name")
                recruiter_title_el = node.select_one("span.hirer-card__hirer-subtitle")

                cards.append(
                    {
                        "job_id": node.get("data-entity-urn", "").split(":")[-1] or None,
                        "title": title_el.get_text(strip=True) if title_el else "",
                        "company": comp_el.get_text(strip=True) if comp_el else "",
                        "location": loc_el.get_text(strip=True) if loc_el else "Remote",
                        "job_url": link_el.get("href", "") if link_el else "",
                        "description": desc_el.get_text(" ", strip=True) if desc_el else "",
                        "experience": "",
                        "seniority_badge": (
                            seniority_el.get_text(strip=True) if seniority_el else ""
                        ),
                        "recruiter_name": (
                            recruiter_el.get_text(strip=True) if recruiter_el else None
                        ),
                        "recruiter_title": (
                            recruiter_title_el.get_text(strip=True)
                            if recruiter_title_el
                            else None
                        ),
                    }
                )
            return cards
        except ImportError:
            return self._regex_parse_linkedin_cards(html_content)

    @staticmethod
    def _regex_parse_linkedin_cards(html_content: str) -> List[Dict[str, any]]:
        cards: List[Dict[str, any]] = []
        blocks = re.split(
            r'(?=<(?:div|li)[^>]*class="[^"]*(?:base-card|job-search-card|jobs-search-results__list-item)[^"]*")',
            html_content,
            flags=re.IGNORECASE,
        )
        for block in blocks:
            if not re.search(
                r'class="[^"]*(?:base-card|job-search-card|jobs-search-results__list-item)',
                block,
                flags=re.IGNORECASE,
            ):
                continue
            title_m = re.search(
                r'<h3[^>]*class="[^"]*base-search-card__title[^"]*"[^>]*>(.*?)</h3>',
                block,
                flags=re.DOTALL | re.IGNORECASE,
            )
            comp_m = re.search(
                r'<h4[^>]*class="[^"]*base-search-card__subtitle[^"]*"[^>]*>(.*?)</h4>',
                block,
                flags=re.DOTALL | re.IGNORECASE,
            )
            if not title_m or not comp_m:
                continue
            loc_m = re.search(
                r'<span[^>]*class="[^"]*job-search-card__location[^"]*"[^>]*>(.*?)</span>',
                block,
                flags=re.DOTALL | re.IGNORECASE,
            )
            desc_m = re.search(
                r'<(?:div|p)[^>]*class="[^"]*(?:show-more-less-html__markup|job-description)[^"]*"[^>]*>(.*?)</(?:div|p)>',
                block,
                flags=re.DOTALL | re.IGNORECASE,
            )
            sen_m = re.search(
                r'<span[^>]*class="[^"]*(?:description__job-criteria-text|seniority-badge)[^"]*"[^>]*>(.*?)</span>',
                block,
                flags=re.DOTALL | re.IGNORECASE,
            )
            cards.append(
                {
                    "job_id": None,
                    "title": re.sub(r"<[^>]+>", "", title_m.group(1)).strip(),
                    "company": re.sub(r"<[^>]+>", "", comp_m.group(1)).strip(),
                    "location": (
                        re.sub(r"<[^>]+>", "", loc_m.group(1)).strip()
                        if loc_m
                        else "Remote"
                    ),
                    "job_url": "",
                    "description": (
                        re.sub(r"<[^>]+>", " ", desc_m.group(1)).strip()
                        if desc_m
                        else ""
                    ),
                    "experience": "",
                    "seniority_badge": (
                        re.sub(r"<[^>]+>", "", sen_m.group(1)).strip()
                        if sen_m
                        else ""
                    ),
                    "recruiter_name": None,
                    "recruiter_title": None,
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
        """Scrapes LinkedIn Jobs search pages using Playwright and filters results."""
        if self._context is None:
            await self.start_browser()

        pages_to_scrape = max_pages or self.max_pages
        role_cat = infer_role_category(role_keyword)
        collected: List[JobPosting] = []
        page = await self._context.new_page()

        try:
            for page_idx in range(pages_to_scrape):
                start_offset = page_idx * 25
                url = self.build_search_url(
                    role_keyword=role_keyword,
                    location=location,
                    start_offset=start_offset,
                )
                logger.info("Scraping LinkedIn URL: %s", url)
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await self.human_delay()
                await self.human_scroll(page, steps=4)
                html = await page.content()
                page_jobs = self.parse_html_cards(html, role_category=role_cat)
                collected.extend(page_jobs)
        finally:
            await page.close()

        return collected

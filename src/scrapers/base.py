"""Base Playwright stealth scraper with anti-bot jitter, UA rotation, and retry logic."""

from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from src.models import JobPosting
from src.scrapers.exp_filter import ExperienceFilter

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4] });
window.chrome = { runtime: {} };
"""


class BaseJobScraper(ABC):
    """Abstract base class for Playwright-based job board scrapers."""

    platform_name: str = "base"

    def __init__(
        self,
        exp_filter: Optional[ExperienceFilter] = None,
        headless: bool = True,
        min_delay_sec: float = 2.0,
        max_delay_sec: float = 5.0,
        max_pages: int = 2,
    ) -> None:
        self.exp_filter = exp_filter or ExperienceFilter()
        self.headless = headless
        self.min_delay_sec = min_delay_sec
        self.max_delay_sec = max_delay_sec
        self.max_pages = max_pages
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None

    async def __aenter__(self) -> "BaseJobScraper":
        await self.start_browser()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close_browser()

    async def start_browser(self) -> None:
        """Launches Chromium with anti-automation stealth scripts and randomized viewport."""
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is required for live browser scraping. "
                "Install via `pip install playwright && playwright install chromium`."
            ) from exc

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        user_agent = random.choice(USER_AGENTS)
        viewport = {
            "width": random.choice([1366, 1440, 1536, 1920]),
            "height": random.choice([768, 900, 1080]),
        }
        self._context = await self._browser.new_context(
            user_agent=user_agent,
            viewport=viewport,
            locale="en-US",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        await self._context.add_init_script(STEALTH_INIT_SCRIPT)

    async def close_browser(self) -> None:
        """Closes Playwright context and browser cleanly."""
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def human_delay(
        self,
        min_sec: Optional[float] = None,
        max_sec: Optional[float] = None,
    ) -> None:
        """Sleeps for a randomized human-like duration to avoid anti-bot rate limits."""
        low = self.min_delay_sec if min_sec is None else min_sec
        high = self.max_delay_sec if max_sec is None else max_sec
        delay = random.uniform(low, max(low, high))
        await asyncio.sleep(delay)

    async def human_scroll(self, page: Any, steps: int = 4) -> None:
        """Scrolls down incrementally to trigger lazy-loaded DOM elements."""
        for _ in range(steps):
            distance = random.randint(350, 750)
            await page.evaluate(f"window.scrollBy(0, {distance});")
            await asyncio.sleep(random.uniform(0.4, 1.1))

    @abstractmethod
    async def scrape_role(
        self, role_keyword: str, location: str, max_pages: Optional[int] = None
    ) -> List[JobPosting]:
        """Scrapes and filters entry-level jobs for a specific role and location."""
        raise NotImplementedError

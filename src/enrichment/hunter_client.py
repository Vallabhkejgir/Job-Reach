"""Hunter.io REST API v2 client for Domain Search, Email Finder, and Email Verifier."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.enrichment.apollo_client import (
    EnrichmentAPIError,
    QuotaExhaustedError,
    RateLimitError,
)
from src.models import DecisionMaker

logger = logging.getLogger(__name__)


class HunterClient:
    """Client for Hunter.io v2 `/domain-search`, `/email-finder`, and `/email-verifier`."""

    BASE_URL = "https://api.hunter.io/v2"

    PRIORITY_ROLE_KEYWORDS = (
        "recruit",
        "talent",
        "hr",
        "human resources",
        "people",
        "hiring",
        "engineering manager",
        "cto",
        "founder",
        "head of engineering",
        "vp engineering",
        "director of engineering",
    )

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout_sec: float = 15.0,
        http_transport: Optional[
            Callable[[str, str, Dict[str, Any], Dict[str, str]], Tuple[int, Dict[str, str], Dict[str, Any]]]
        ] = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.timeout_sec = timeout_sec
        self._http_transport = http_transport
        self.last_remaining_credits: Optional[int] = None
        self.last_discovered_pattern: Optional[str] = None

    @property
    def is_configured(self) -> bool:
        return bool(
            self.api_key
            and not self.api_key.startswith("your_")
            and self.api_key != "placeholder"
        )

    def _get(
        self, endpoint: str, params: Dict[str, Any]
    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
        if not self.is_configured:
            raise QuotaExhaustedError(
                "hunter", "HUNTER_API_KEY is not configured", status_code=401
            )

        query_params = {**params, "api_key": self.api_key}
        url = f"{self.BASE_URL}{endpoint}?{urllib.parse.urlencode(query_params)}"
        headers = {"Accept": "application/json", "User-Agent": "JobReach/1.0"}

        if self._http_transport is not None:
            status_code, resp_headers, data = self._http_transport(
                "GET", url, query_params, headers
            )
            self._inspect_status_and_headers(status_code, resp_headers, data)
            return resp_headers, data

        try:
            import httpx

            with httpx.Client(timeout=self.timeout_sec) as client:
                response = client.get(url, headers=headers)
                resp_headers = {k.lower(): v for k, v in response.headers.items()}
                try:
                    data = response.json()
                except Exception:
                    data = {"raw": response.text}
                self._inspect_status_and_headers(
                    response.status_code, resp_headers, data
                )
                return resp_headers, data
        except ImportError:
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                    status_code = resp.getcode()
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    raw_body = resp.read().decode("utf-8")
                    data = json.loads(raw_body) if raw_body else {}
                    self._inspect_status_and_headers(status_code, resp_headers, data)
                    return resp_headers, data
            except urllib.error.HTTPError as err:
                resp_headers = (
                    {k.lower(): v for k, v in err.headers.items()}
                    if err.headers
                    else {}
                )
                try:
                    err_body = err.read().decode("utf-8")
                    data = json.loads(err_body) if err_body else {}
                except Exception:
                    data = {}
                self._inspect_status_and_headers(err.code, resp_headers, data)
                raise EnrichmentAPIError(f"Hunter HTTP {err.code}") from err

    def _inspect_status_and_headers(
        self,
        status_code: int,
        headers: Dict[str, str],
        data: Dict[str, Any],
    ) -> None:
        lower_headers = {k.lower(): str(v) for k, v in (headers or {}).items()}
        if "x-ratelimit-remaining" in lower_headers:
            try:
                self.last_remaining_credits = int(
                    lower_headers["x-ratelimit-remaining"]
                )
            except ValueError:
                pass

        errors = data.get("errors") or []
        err_text = "; ".join(
            str(e.get("details") or e.get("id") or e) for e in errors
        )
        if status_code == 429:
            raise RateLimitError(
                "hunter",
                f"Hunter.io rate/quota limit reached: {err_text or 'HTTP 429'}",
                status_code=429,
            )
        if status_code in (401, 402, 403):
            raise QuotaExhaustedError(
                "hunter",
                f"Hunter.io quota/auth error (HTTP {status_code}): {err_text}",
                status_code=status_code,
            )
        if any(
            tok in err_text.lower()
            for tok in ("too_many_requests", "quota", "limit", "no_credits")
        ):
            raise QuotaExhaustedError(
                "hunter",
                f"Hunter.io free credits exhausted: {err_text}",
                status_code=429,
            )
        if status_code >= 400:
            raise EnrichmentAPIError(
                f"Hunter API error HTTP {status_code}: {err_text}"
            )

    def find_email(
        self,
        company_domain: str,
        first_name: str,
        last_name: str,
        company_name: str = "",
        title: str = "Hiring Manager",
        linkedin_url: Optional[str] = None,
    ) -> Optional[DecisionMaker]:
        """Calls `/v2/email-finder` to resolve a known decision maker's email address."""
        params = {
            "domain": company_domain,
            "first_name": first_name,
            "last_name": last_name,
        }
        _, payload = self._get("/email-finder", params)
        data = payload.get("data") or {}
        email = (data.get("email") or "").strip().lower()
        if not email or "@" not in email:
            return None

        score = int(data.get("score") or 80)
        verification = data.get("verification") or {}
        verified = verification.get("status") == "valid" or score >= 85
        full_name = f"{first_name} {last_name}".strip()

        return DecisionMaker(
            full_name=full_name,
            first_name=data.get("first_name") or first_name,
            last_name=data.get("last_name") or last_name,
            title=data.get("position") or title,
            company=company_name or data.get("company") or company_domain,
            company_domain=company_domain,
            email=email,
            linkedin_url=linkedin_url or data.get("linkedin_url"),
            source_provider="hunter",
            confidence_score=score,
            verified=verified,
        )

    def domain_search(
        self,
        company_domain: str,
        company_name: str = "",
        limit: int = 10,
    ) -> Tuple[List[DecisionMaker], Optional[str]]:
        """Calls `/v2/domain-search` to discover contacts and company email pattern."""
        params = {
            "domain": company_domain,
            "limit": limit,
        }
        _, payload = self._get("/domain-search", params)
        data = payload.get("data") or {}
        pattern = data.get("pattern")
        if pattern:
            self.last_discovered_pattern = str(pattern)

        emails = data.get("emails") or []
        candidates: List[DecisionMaker] = []

        for entry in emails:
            email_val = (entry.get("value") or "").strip().lower()
            if not email_val or "@" not in email_val:
                continue
            first_name = (entry.get("first_name") or "").strip()
            last_name = (entry.get("last_name") or "").strip()
            full_name = f"{first_name} {last_name}".strip() or "Hiring Team"
            position = (
                entry.get("position")
                or entry.get("department")
                or "Talent Acquisition"
            ).strip()
            confidence = int(entry.get("confidence") or 75)
            ver_status = (entry.get("verification") or {}).get("status")

            candidates.append(
                DecisionMaker(
                    full_name=full_name,
                    first_name=first_name or "Hiring",
                    last_name=last_name or "Team",
                    title=position,
                    company=company_name or data.get("organization") or company_domain,
                    company_domain=company_domain,
                    email=email_val,
                    linkedin_url=entry.get("linkedin"),
                    source_provider="hunter",
                    confidence_score=confidence,
                    verified=(ver_status == "valid") or confidence >= 85,
                )
            )

        # Sort so recruiters / hiring managers / engineering leads come first
        candidates.sort(
            key=lambda dm: (
                any(
                    kw in dm.title.lower()
                    for kw in self.PRIORITY_ROLE_KEYWORDS
                ),
                dm.verified,
                dm.confidence_score,
            ),
            reverse=True,
        )
        return candidates, pattern

    def verify_email(self, email: str) -> bool:
        """Calls `/v2/email-verifier` to validate deliverability of an email address."""
        _, payload = self._get("/email-verifier", {"email": email})
        data = payload.get("data") or {}
        status = (data.get("status") or "").lower()
        result = (data.get("result") or "").lower()
        return status in ("valid", "accept_all") or result == "deliverable"

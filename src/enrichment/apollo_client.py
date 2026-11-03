"""Apollo.io REST API v1 client for decision-maker discovery and email unlocking."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.models import DecisionMaker

logger = logging.getLogger(__name__)


class EnrichmentAPIError(Exception):
    """Base exception for enrichment API failures."""


class QuotaExhaustedError(EnrichmentAPIError):
    """Raised when a provider's free-tier credit quota or rate limit is exhausted."""

    def __init__(self, provider: str, message: str, status_code: int = 429) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.status_code = status_code


class RateLimitError(QuotaExhaustedError):
    """Raised specifically on HTTP 429 rate-limit responses."""


LOCKED_EMAIL_PLACEHOLDERS = {
    "email_not_unlocked@domain.com",
    "not_unlocked@domain.com",
    "locked@apollo.io",
}


class ApolloClient:
    """Client for Apollo.io People Search and People Match APIs."""

    BASE_URL = "https://api.apollo.io/v1"

    DEFAULT_TARGET_TITLES = [
        "Technical Recruiter",
        "Talent Acquisition",
        "Engineering Manager",
        "Hiring Manager",
        "Head of Talent",
        "Recruiter",
        "HR Manager",
        "CTO",
        "Founder",
    ]

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

    @property
    def is_configured(self) -> bool:
        return bool(
            self.api_key
            and not self.api_key.startswith("your_")
            and self.api_key != "placeholder"
        )

    def _request(
        self, method: str, endpoint: str, payload: Dict[str, Any]
    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
        if not self.is_configured:
            raise QuotaExhaustedError(
                "apollo", "APOLLO_API_KEY is not configured", status_code=401
            )

        url = f"{self.BASE_URL}{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "X-Api-Key": self.api_key,
            "User-Agent": "JobReach/1.0",
        }

        if self._http_transport is not None:
            status_code, resp_headers, data = self._http_transport(
                method, url, payload, headers
            )
            self._inspect_status_and_headers(status_code, resp_headers, data)
            return resp_headers, data

        try:
            import httpx

            with httpx.Client(timeout=self.timeout_sec) as client:
                response = client.request(method, url, json=payload, headers=headers)
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
            body_bytes = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=body_bytes, headers=headers, method=method
            )
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
                raise EnrichmentAPIError(f"Apollo HTTP {err.code}") from err

    def _inspect_status_and_headers(
        self,
        status_code: int,
        headers: Dict[str, str],
        data: Dict[str, Any],
    ) -> None:
        lower_headers = {k.lower(): str(v) for k, v in (headers or {}).items()}
        for header_name in (
            "x-rate-limit-remaining",
            "x-24-hour-usage-left",
            "x-CREDITS-remaining".lower(),
        ):
            if header_name in lower_headers:
                try:
                    self.last_remaining_credits = int(lower_headers[header_name])
                except ValueError:
                    pass

        err_msg = str(data.get("error") or data.get("message") or "")
        if status_code == 429:
            raise RateLimitError(
                "apollo",
                f"Apollo rate limit or free credit ceiling reached: {err_msg or 'HTTP 429'}",
                status_code=429,
            )
        if status_code in (401, 402, 403):
            raise QuotaExhaustedError(
                "apollo",
                f"Apollo quota/auth error (HTTP {status_code}): {err_msg}",
                status_code=status_code,
            )
        if any(
            phrase in err_msg.lower()
            for phrase in ("insufficient credits", "credit limit", "quota exceeded")
        ):
            raise QuotaExhaustedError(
                "apollo",
                f"Apollo free-tier credits exhausted: {err_msg}",
                status_code=402,
            )
        if status_code >= 400:
            raise EnrichmentAPIError(
                f"Apollo API error HTTP {status_code}: {err_msg}"
            )

    @staticmethod
    def _is_valid_unlocked_email(email: Optional[str]) -> bool:
        if not email or "@" not in email:
            return False
        cleaned = email.strip().lower()
        if cleaned in LOCKED_EMAIL_PLACEHOLDERS:
            return False
        if "email_not_unlocked" in cleaned or "*" in cleaned:
            return False
        return True

    def search_decision_makers(
        self,
        company_domain: str,
        company_name: str = "",
        target_titles: Optional[List[str]] = None,
        per_page: int = 5,
    ) -> List[DecisionMaker]:
        """Searches Apollo for recruiters or engineering managers at the target company."""
        titles = target_titles or self.DEFAULT_TARGET_TITLES
        payload = {
            "q_organization_domains": company_domain,
            "person_titles": titles,
            "page": 1,
            "per_page": per_page,
        }
        _, data = self._request("POST", "/mixed_people/search", payload)
        people = data.get("people") or data.get("contacts") or []
        results: List[DecisionMaker] = []

        for person in people:
            first_name = (person.get("first_name") or "").strip()
            last_name = (person.get("last_name") or "").strip()
            full_name = (
                person.get("name") or f"{first_name} {last_name}"
            ).strip()
            if not full_name:
                continue

            raw_email = person.get("email")
            unlocked_email = (
                raw_email.strip().lower()
                if self._is_valid_unlocked_email(raw_email)
                else None
            )
            org = person.get("organization") or {}
            domain = (
                org.get("primary_domain") or company_domain or ""
            ).strip().lower()

            results.append(
                DecisionMaker(
                    full_name=full_name,
                    first_name=first_name or full_name.split()[0],
                    last_name=last_name
                    or (full_name.split()[-1] if len(full_name.split()) > 1 else ""),
                    title=(person.get("title") or "Hiring Manager").strip(),
                    company=company_name or org.get("name") or domain,
                    company_domain=domain,
                    email=unlocked_email,
                    linkedin_url=person.get("linkedin_url"),
                    source_provider="apollo",
                    confidence_score=92 if unlocked_email else 60,
                    verified=bool(
                        unlocked_email
                        and person.get("email_status") in ("verified", "valid")
                    ),
                )
            )
        return results

    def unlock_person_email(
        self,
        first_name: str,
        last_name: str,
        company_domain: str,
        company_name: str = "",
        title: str = "Hiring Manager",
        linkedin_url: Optional[str] = None,
    ) -> Optional[DecisionMaker]:
        """Calls `/v1/people/match` to unlock a specific decision maker's business email."""
        payload: Dict[str, Any] = {
            "first_name": first_name,
            "last_name": last_name,
            "domain": company_domain,
            "reveal_personal_emails": False,
        }
        if linkedin_url:
            payload["linkedin_url"] = linkedin_url

        _, data = self._request("POST", "/people/match", payload)
        person = data.get("person") or {}
        if not person:
            return None

        raw_email = person.get("email")
        unlocked_email = (
            raw_email.strip().lower()
            if self._is_valid_unlocked_email(raw_email)
            else None
        )
        if not unlocked_email:
            return None

        full_name = (
            person.get("name") or f"{first_name} {last_name}"
        ).strip()
        return DecisionMaker(
            full_name=full_name,
            first_name=person.get("first_name") or first_name,
            last_name=person.get("last_name") or last_name,
            title=person.get("title") or title,
            company=company_name
            or (person.get("organization") or {}).get("name")
            or company_domain,
            company_domain=company_domain,
            email=unlocked_email,
            linkedin_url=person.get("linkedin_url") or linkedin_url,
            source_provider="apollo",
            confidence_score=95,
            verified=person.get("email_status") in ("verified", "valid", None),
        )

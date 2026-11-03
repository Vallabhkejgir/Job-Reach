"""Pattern permutation and DNS MX record fallback engine when API credits are exhausted."""

from __future__ import annotations

import logging
import re
from typing import Callable, List, Optional

from src.models import DecisionMaker

logger = logging.getLogger(__name__)

COMPANY_SUFFIXES = re.compile(
    r"(?i)\b(pvt\.?\s*ltd\.?|private\s+limited|limited|ltd\.?|inc\.?|llc|llp|corp\.?|corporation|technologies|technology|tech|solutions|systems|software|labs|india|global|group|services)\b"
)


def infer_company_domain(company_name: str, explicit_domain: Optional[str] = None) -> str:
    """Infers a canonical corporate domain from a company name when not explicitly provided."""
    if explicit_domain and "." in explicit_domain:
        cleaned = explicit_domain.lower().strip()
        cleaned = re.sub(r"^https?://", "", cleaned).split("/")[0]
        if cleaned.startswith("www."):
            cleaned = cleaned[4:]
        return cleaned

    raw = (company_name or "").strip().lower()
    if "." in raw and " " not in raw:
        return raw

    stripped = COMPANY_SUFFIXES.sub("", raw)
    slug = re.sub(r"[^a-z0-9]", "", stripped)
    if not slug:
        slug = re.sub(r"[^a-z0-9]", "", raw) or "company"
    return f"{slug}.com"


class PatternFallbackEngine:
    """Generates heuristic business emails and validates domain MX records."""

    def __init__(
        self,
        mx_Lookup_fn: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self._mx_lookup_fn = mx_Lookup_fn
        self._mx_cache: dict[str, bool] = {}

    def verify_mx_record(self, domain: str) -> bool:
        """Verifies that a domain has valid DNS MX records capable of receiving mail."""
        clean_domain = (domain or "").strip().lower()
        if not clean_domain or "." not in clean_domain:
            return False

        if clean_domain in self._mx_cache:
            return self._mx_cache[clean_domain]

        if self._mx_lookup_fn is not None:
            res = bool(self._mx_lookup_fn(clean_domain))
            self._mx_cache[clean_domain] = res
            return res

        try:
            import dns.resolver

            answers = dns.resolver.resolve(clean_domain, "MX", lifetime=5.0)
            valid = len(answers) > 0
            self._mx_cache[clean_domain] = valid
            return valid
        except ImportError:
            # When dnspython is not installed in an offline environment, validate syntax
            valid = bool(
                re.match(r"^[a-z0-9][a-z0-9\-\.]+\.[a-z]{2,}$", clean_domain)
            )
            self._mx_cache[clean_domain] = valid
            return valid
        except Exception as exc:
            logger.debug("MX lookup failed for %s: %s", clean_domain, exc)
            self._mx_cache[clean_domain] = False
            return False

    @staticmethod
    def apply_pattern(
        pattern: str, first_name: str, last_name: str, domain: str
    ) -> str:
        """Applies a Hunter-style pattern (e.g. '{first}.{last}') to a person's name."""
        f = re.sub(r"[^a-z0-9]", "", (first_name or "").lower())
        l = re.sub(r"[^a-z0-9]", "", (last_name or "").lower())
        if not f:
            return f"careers@{domain}"

        local = (
            pattern.replace("{first}", f)
            .replace("{last}", l)
            .replace("{f}", f[:1])
            .replace("{l}", l[:1] if l else "")
            .strip("._-")
        )
        return f"{local}@{domain}"

    def generate_permutations(
        self,
        company_domain: str,
        first_name: str = "",
        last_name: str = "",
        known_pattern: Optional[str] = None,
    ) -> List[str]:
        """Generates ordered candidate email addresses for a decision maker or hiring team."""
        domain = company_domain.strip().lower()
        f = re.sub(r"[^a-z0-9]", "", (first_name or "").lower())
        l = re.sub(r"[^a-z0-9]", "", (last_name or "").lower())

        candidates: List[str] = []
        if f and known_pattern:
            candidates.append(self.apply_pattern(known_pattern, f, l, domain))

        if f and l:
            for perm in (
                f"{f}.{l}@{domain}",
                f"{f}@{domain}",
                f"{f[:1]}{l}@{domain}",
                f"{f}_{l}@{domain}",
                f"{f}{l}@{domain}",
            ):
                if perm not in candidates:
                    candidates.append(perm)
        elif f:
            candidates.append(f"{f}@{domain}")

        for alias in (
            f"careers@{domain}",
            f"talent@{domain}",
            f"hiring@{domain}",
            f"hr@{domain}",
        ):
            if alias not in candidates:
                candidates.append(alias)

        return candidates

    def build_fallback_contact(
        self,
        company: str,
        company_domain: str,
        full_name: Optional[str] = None,
        title: str = "Talent Acquisition Team",
        known_pattern: Optional[str] = None,
    ) -> Optional[DecisionMaker]:
        """Constructs a fallback DecisionMaker if the company domain has a valid MX record."""
        if not self.verify_mx_record(company_domain):
            return None

        first_name = ""
        last_name = ""
        cleaned_name = (full_name or "").strip()
        if cleaned_name and cleaned_name.lower() not in (
            "hiring team",
            "talent acquisition",
            "unknown",
        ):
            parts = cleaned_name.split()
            first_name = parts[0]
            last_name = parts[-1] if len(parts) > 1 else ""
        else:
            cleaned_name = f"{company} Hiring Team"
            first_name = "Hiring"
            last_name = "Team"

        perms = self.generate_permutations(
            company_domain=company_domain,
            first_name="" if first_name == "Hiring" else first_name,
            last_name="" if last_name == "Team" else last_name,
            known_pattern=known_pattern,
        )
        if not perms:
            return None

        return DecisionMaker(
            full_name=cleaned_name,
            first_name=first_name,
            last_name=last_name,
            title=title,
            company=company,
            company_domain=company_domain,
            email=perms[0],
            linkedin_url=None,
            source_provider="pattern_mx_fallback",
            confidence_score=65 if first_name != "Hiring" else 50,
            verified=False,
        )

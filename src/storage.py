"""SQLite WAL state persistence, zero-credit domain cache, and API quota ledger."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.models import (
    DecisionMaker,
    ExperienceCategory,
    JobPosting,
    OutreachDraft,
    ProviderQuotaStatus,
)


class StorageManager:
    """Manages SQLite state for scraped jobs, enriched contacts, quotas, and outreach logs."""

    DEFAULT_QUOTAS = {
        "apollo": 50,
        "hunter": 25,
    }

    def __init__(
        self,
        db_path: str | Path = "job_reach_state.db",
        apollo_monthly_limit: int = 50,
        hunter_monthly_limit: int = 25,
    ) -> None:
        self.db_path = str(db_path)
        self.default_limits = {
            "apollo": apollo_monthly_limit,
            "hunter": hunter_monthly_limit,
        }
        self._persistent_conn: Optional[sqlite3.Connection] = None
        if self.db_path == ":memory:":
            self._persistent_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._persistent_conn.row_factory = sqlite3.Row
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        if self._persistent_conn is not None:
            return self._persistent_conn
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _close(self, conn: sqlite3.Connection) -> None:
        if conn is not self._persistent_conn:
            conn.close()

    def init_db(self) -> None:
        """Creates all required tables and seeds default free-tier quota rows."""
        conn = self._connect()
        try:
            with conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY,
                        platform TEXT NOT NULL,
                        title TEXT NOT NULL,
                        company TEXT NOT NULL,
                        location TEXT NOT NULL,
                        experience_raw TEXT,
                        min_years REAL,
                        max_years REAL,
                        experience_category TEXT NOT NULL,
                        is_edge_case INTEGER NOT NULL DEFAULT 0,
                        role_category TEXT NOT NULL,
                        job_url TEXT,
                        company_domain TEXT,
                        recruiter_name TEXT,
                        recruiter_title TEXT,
                        description_snippet TEXT,
                        seniority_badge TEXT,
                        skills_json TEXT,
                        status TEXT NOT NULL DEFAULT 'NEW',
                        posted_at TEXT,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS contacts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        company TEXT NOT NULL,
                        company_domain TEXT NOT NULL,
                        full_name TEXT NOT NULL,
                        first_name TEXT,
                        last_name TEXT,
                        title TEXT,
                        email TEXT UNIQUE,
                        linkedin_url TEXT,
                        source_provider TEXT NOT NULL,
                        confidence_score INTEGER NOT NULL DEFAULT 0,
                        verified INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_contacts_domain
                        ON contacts(company_domain);
                    CREATE INDEX IF NOT EXISTS idx_contacts_company
                        ON contacts(LOWER(company));

                    CREATE TABLE IF NOT EXISTS company_patterns (
                        company_domain TEXT PRIMARY KEY,
                        company TEXT NOT NULL,
                        email_pattern TEXT NOT NULL,
                        mx_valid INTEGER NOT NULL DEFAULT 1,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS api_quota_ledger (
                        provider TEXT PRIMARY KEY,
                        credits_remaining INTEGER NOT NULL,
                        monthly_limit INTEGER NOT NULL,
                        exhausted INTEGER NOT NULL DEFAULT 0,
                        reset_at TEXT,
                        last_error TEXT,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS outreach_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL,
                        recipient_email TEXT NOT NULL,
                        recipient_name TEXT,
                        company TEXT NOT NULL,
                        role_title TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        body_text TEXT NOT NULL,
                        transport TEXT NOT NULL,
                        status TEXT NOT NULL,
                        sent_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_outreach_recipient
                        ON outreach_log(LOWER(recipient_email), status, sent_at);
                    """
                )
            for provider, limit in self.default_limits.items():
                self.init_provider_quota(provider, limit)
        finally:
            self._close(conn)

    # ------------------------------------------------------------------
    # Job Posting Persistence
    # ------------------------------------------------------------------

    def upsert_job(self, job: JobPosting, status: str = "NEW") -> bool:
        """Inserts or updates a scraped job posting. Returns True if newly inserted."""
        conn = self._connect()
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            existing = conn.execute(
                "SELECT job_id FROM jobs WHERE job_id = ?", (job.job_id,)
            ).fetchone()
            with conn:
                conn.execute(
                    """
                    INSERT INTO jobs (
                        job_id, platform, title, company, location,
                        experience_raw, min_years, max_years, experience_category,
                        is_edge_case, role_category, job_url, company_domain,
                        recruiter_name, recruiter_title, description_snippet,
                        seniority_badge, skills_json, status, posted_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        title = excluded.title,
                        experience_raw = excluded.experience_raw,
                        min_years = excluded.min_years,
                        max_years = excluded.max_years,
                        experience_category = excluded.experience_category,
                        is_edge_case = excluded.is_edge_case,
                        company_domain = COALESCE(excluded.company_domain, jobs.company_domain),
                        recruiter_name = COALESCE(excluded.recruiter_name, jobs.recruiter_name),
                        recruiter_title = COALESCE(excluded.recruiter_title, jobs.recruiter_title),
                        description_snippet = excluded.description_snippet
                    """,
                    (
                        job.job_id,
                        job.platform,
                        job.title,
                        job.company,
                        job.location,
                        job.experience_raw,
                        job.min_years,
                        job.max_years,
                        job.experience_category.value
                        if isinstance(job.experience_category, ExperienceCategory)
                        else str(job.experience_category),
                        1 if job.is_edge_case else 0,
                        job.role_category,
                        job.job_url,
                        job.company_domain,
                        job.recruiter_name,
                        job.recruiter_title,
                        job.description_snippet,
                        job.seniority_badge,
                        json.dumps(job.skills or []),
                        status,
                        job.posted_at,
                        now_iso,
                    ),
                )
            return existing is None
        finally:
            self._close(conn)

    def get_jobs(
        self, status: Optional[str] = None, limit: int = 100
    ) -> List[JobPosting]:
        """Retrieves stored job postings, optionally filtered by status."""
        conn = self._connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()

            results: List[JobPosting] = []
            for row in rows:
                cat_val = row["experience_category"] or "UNLISTED"
                try:
                    cat_enum = ExperienceCategory(cat_val)
                except ValueError:
                    cat_enum = ExperienceCategory.UNLISTED

                skills = []
                if row["skills_json"]:
                    try:
                        skills = json.loads(row["skills_json"])
                    except json.JSONDecodeError:
                        skills = []

                results.append(
                    JobPosting(
                        job_id=row["job_id"],
                        platform=row["platform"],
                        title=row["title"],
                        company=row["company"],
                        location=row["location"],
                        experience_raw=row["experience_raw"] or "",
                        min_years=row["min_years"],
                        max_years=row["max_years"],
                        experience_category=cat_enum,
                        is_edge_case=bool(row["is_edge_case"]),
                        role_category=row["role_category"],
                        job_url=row["job_url"] or "",
                        company_domain=row["company_domain"],
                        recruiter_name=row["recruiter_name"],
                        recruiter_title=row["recruiter_title"],
                        description_snippet=row["description_snippet"] or "",
                        seniority_badge=row["seniority_badge"] or "",
                        skills=skills,
                        posted_at=row["posted_at"] or "",
                    )
                )
            return results
        finally:
            self._close(conn)

    def update_job_status(self, job_id: str, status: str) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE jobs SET status = ? WHERE job_id = ?",
                    (status, job_id),
                )
        finally:
            self._close(conn)

    # ------------------------------------------------------------------
    # Contact & Domain Pattern Cache (Zero-Credit Lookup)
    # ------------------------------------------------------------------

    def save_contact(self, dm: DecisionMaker) -> None:
        """Persists an enriched decision maker contact into the SQLite cache."""
        if not dm.email:
            return
        conn = self._connect()
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO contacts (
                        company, company_domain, full_name, first_name, last_name,
                        title, email, linkedin_url, source_provider,
                        confidence_score, verified, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(email) DO UPDATE SET
                        title = excluded.title,
                        source_provider = excluded.source_provider,
                        confidence_score = MAX(contacts.confidence_score, excluded.confidence_score),
                        verified = MAX(contacts.verified, excluded.verified)
                    """,
                    (
                        dm.company,
                        dm.company_domain.lower().strip(),
                        dm.full_name,
                        dm.first_name,
                        dm.last_name,
                        dm.title,
                        dm.email.lower().strip(),
                        dm.linkedin_url,
                        dm.source_provider,
                        dm.confidence_score,
                        1 if dm.verified else 0,
                        now_iso,
                    ),
                )
        finally:
            self._close(conn)

    def get_cached_contact(
        self,
        company: Optional[str] = None,
        company_domain: Optional[str] = None,
    ) -> Optional[DecisionMaker]:
        """Looks up a previously enriched decision maker for a domain or company (0 API credits)."""
        conn = self._connect()
        try:
            row = None
            if company_domain:
                row = conn.execute(
                    """
                    SELECT * FROM contacts
                    WHERE LOWER(company_domain) = LOWER(?)
                      AND email IS NOT NULL
                    ORDER BY verified DESC, confidence_score DESC, id DESC
                    LIMIT 1
                    """,
                    (company_domain.strip(),),
                ).fetchone()
            if row is None and company:
                row = conn.execute(
                    """
                    SELECT * FROM contacts
                    WHERE LOWER(company) = LOWER(?)
                      AND email IS NOT NULL
                    ORDER BY verified DESC, confidence_score DESC, id DESC
                    LIMIT 1
                    """,
                    (company.strip(),),
                ).fetchone()
            if row is None:
                return None

            return DecisionMaker(
                full_name=row["full_name"],
                first_name=row["first_name"] or "",
                last_name=row["last_name"] or "",
                title=row["title"] or "",
                company=row["company"],
                company_domain=row["company_domain"],
                email=row["email"],
                linkedin_url=row["linkedin_url"],
                source_provider=row["source_provider"],
                confidence_score=int(row["confidence_score"] or 0),
                verified=bool(row["verified"]),
            )
        finally:
            self._close(conn)

    def save_company_pattern(
        self,
        company_domain: str,
        company: str,
        email_pattern: str,
        mx_valid: bool = True,
    ) -> None:
        conn = self._connect()
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO company_patterns (
                        company_domain, company, email_pattern, mx_valid, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(company_domain) DO UPDATE SET
                        email_pattern = excluded.email_pattern,
                        mx_valid = excluded.mx_valid,
                        updated_at = excluded.updated_at
                    """,
                    (
                        company_domain.lower().strip(),
                        company,
                        email_pattern,
                        1 if mx_valid else 0,
                        now_iso,
                    ),
                )
        finally:
            self._close(conn)

    def get_company_pattern(self, company_domain: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM company_patterns WHERE LOWER(company_domain) = LOWER(?)",
                (company_domain.strip(),),
            ).fetchone()
            return dict(row) if row else None
        finally:
            self._close(conn)

    # ------------------------------------------------------------------
    # API Quota Ledger (Dual Free-Tier Credit Tracking)
    # ------------------------------------------------------------------

    def init_provider_quota(
        self,
        provider: str,
        monthly_limit: int,
        credits_remaining: Optional[int] = None,
    ) -> ProviderQuotaStatus:
        """Initializes a provider quota row if not already present."""
        provider_key = provider.lower().strip()
        initial_credits = (
             monthly_limit if credits_remaining is None else credits_remaining
        )
        now = datetime.now(timezone.utc)
        reset_at = (now + timedelta(days=30)).isoformat()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO api_quota_ledger (
                        provider, credits_remaining, monthly_limit,
                        exhausted, reset_at, last_error, updated_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                    ON CONFLICT(provider) DO NOTHING
                    """,
                    (
                        provider_key,
                        initial_credits,
                        monthly_limit,
                        1 if initial_credits <= 0 else 0,
                        reset_at,
                        now.isoformat(),
                    ),
                )
            return self.get_provider_quota(provider_key)
        finally:
            self._close(conn)

    def get_provider_quota(self, provider: str) -> ProviderQuotaStatus:
        """Returns the current free-tier quota status for Apollo or Hunter."""
        provider_key = provider.lower().strip()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM api_quota_ledger WHERE provider = ?",
                (provider_key,),
            ).fetchone()
            if row is None:
                default_limit = self.default_limits.get(provider_key, 25)
                return self.init_provider_quota(provider_key, default_limit)

            return ProviderQuotaStatus(
                provider=row["provider"],
                credits_remaining=int(row["credits_remaining"]),
                monthly_limit=int(row["monthly_limit"]),
                exhausted=bool(row["exhausted"]) or int(row["credits_remaining"]) <= 0,
                reset_at=row["reset_at"],
                last_error=row["last_error"],
                updated_at=row["updated_at"],
            )
        finally:
            self._close(conn)

    def decrement_provider_quota(
        self, provider: str, amount: int = 1
    ) -> ProviderQuotaStatus:
        """Deducts credits from a provider's ledger and marks exhausted if <= 0."""
        provider_key = provider.lower().strip()
        current = self.get_provider_quota(provider_key)
        new_remaining = max(0, current.credits_remaining - amount)
        exhausted = 1 if new_remaining <= 0 else 0
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE api_quota_ledger
                    SET credits_remaining = ?,
                        exhausted = ?,
                        updated_at = ?
                    WHERE provider = ?
                    """,
                    (new_remaining, exhausted, now_iso, provider_key),
                )
            return self.get_provider_quota(provider_key)
        finally:
            self._close(conn)

    def sync_provider_quota(
        self,
        provider: str,
        credits_remaining: int,
        monthly_limit: Optional[int] = None,
    ) -> ProviderQuotaStatus:
        """Synchronizes SQLite ledger with live HTTP response quota headers/payloads."""
        provider_key = provider.lower().strip()
        current = self.get_provider_quota(provider_key)
        limit_val = monthly_limit if monthly_limit is not None else current.monthly_limit
        remaining_val = max(0, int(credits_remaining))
        exhausted = 1 if remaining_val <= 0 else 0
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE api_quota_ledger
                    SET credits_remaining = ?,
                        monthly_limit = ?,
                        exhausted = ?,
                        updated_at = ?
                    WHERE provider = ?
                    """,
                    (remaining_val, limit_val, exhausted, now_iso, provider_key),
                )
            return self.get_provider_quota(provider_key)
        finally:
            self._close(conn)

    def mark_provider_exhausted(
        self, provider: str, reason: str = "Quota exhausted"
    ) -> ProviderQuotaStatus:
        """Marks an API provider as exhausted (0 credits) so the router fails over immediately."""
        provider_key = provider.lower().strip()
        self.get_provider_quota(provider_key)
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE api_quota_ledger
                    SET credits_remaining = 0,
                        exhausted = 1,
                        last_error = ?,
                        updated_at = ?
                    WHERE provider = ?
                    """,
                    (reason, now_iso, provider_key),
                )
            return self.get_provider_quota(provider_key)
        finally:
            self._close(conn)

    def reset_provider_quota(
        self, provider: str, credits_remaining: Optional[int] = None
    ) -> ProviderQuotaStatus:
        """Resets a provider's quota back to active state."""
        provider_key = provider.lower().strip()
        current = self.get_provider_quota(provider_key)
        restored = (
            current.monthly_limit
            if credits_remaining is None
            else max(0, credits_remaining)
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE api_quota_ledger
                    SET credits_remaining = ?,
                        exhausted = ?,
                        last_error = NULL,
                        updated_at = ?
                    WHERE provider = ?
                    """,
                    (restored, 1 if restored <= 0 else 0, now_iso, provider_key),
                )
            return self.get_provider_quota(provider_key)
        finally:
            self._close(conn)

    # ------------------------------------------------------------------
    # Outreach Deduplication & Audit Log
    # ------------------------------------------------------------------

    def was_contacted_recently(
        self, recipient_email: str, days: int = 30
    ) -> bool:
        """Returns True if a live SENT email was dispatched to recipient_email within `days`."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT id FROM outreach_log
                WHERE LOWER(recipient_email) = LOWER(?)
                  AND status = 'SENT'
                  AND sent_at >= ?
                LIMIT 1
                """,
                (recipient_email.strip(), cutoff),
            ).fetchone()
            return row is not None
        finally:
            self._close(conn)

    def get_daily_send_count(self, day_iso_prefix: Optional[str] = None) -> int:
        """Returns number of live SENT emails recorded for the current UTC date."""
        prefix = day_iso_prefix or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM outreach_log
                WHERE status = 'SENT'
                  AND sent_at LIKE ?
                """,
                (f"{prefix}%",),
            ).fetchone()
            return int(row["cnt"]) if row else 0
        finally:
            self._close(conn)

    def log_outreach(
        self,
        draft: OutreachDraft,
        transport: str,
        status: str = "SENT",
    ) -> None:
        """Records an outreach email event (SENT, DRY_RUN, SKIPPED_DUPLICATE, etc.)."""
        conn = self._connect()
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO outreach_log (
                        job_id, recipient_email, recipient_name, company,
                        role_title, subject, body_text, transport, status, sent_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        draft.job_id,
                        draft.recipient_email.lower().strip(),
                        draft.recipient_name,
                        draft.company,
                        draft.role_title,
                        draft.subject,
                        draft.body_text,
                        transport,
                        status,
                        now_iso,
                    ),
                )
        finally:
            self._close(conn)

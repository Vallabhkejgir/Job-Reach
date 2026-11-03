"""OutreachDispatcher: Dual-transport email sender (SMTP TLS App Password + Gmail OAuth2 API)."""

from __future__ import annotations

import base64
import logging
import os
import random
import smtplib
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, List, Optional

from src.models import OutreachDraft
from src.storage import StorageManager

logger = logging.getLogger(__name__)


@dataclass
class DispatchOutcome:
    """Result of attempting to dispatch a single OutreachDraft."""

    job_id: str
    recipient_email: str
    status: str  # "SENT", "DRY_RUN", "SKIPPED_DUPLICATE", "SKIPPED_DAILY_CAP", "FAILED"
    transport: str
    reason: str = ""


class OutreachDispatcher:
    """Dispatches personalized outreach emails with rate limits, deduplication, and dry-run gating.

    Supported transports:
    1. `smtp`: Standard SMTP over TLS (`smtp.gmail.com:587`) using a 16-character Gmail App Password.
    2. `gmail_oauth`: Official Gmail REST API (`users().messages().send`) via OAuth2 `token.json`.
    """

    def __init__(
        self,
        storage: StorageManager,
        transport: str = "smtp",
        dry_run: bool = True,
        daily_send_cap: int = 25,
        dedup_window_days: int = 30,
        min_jitter_seconds: float = 45.0,
        max_jitter_seconds: float = 120.0,
        sender_name: str = "Alex Rivera",
        sender_email: Optional[str] = None,
        smtp_host: str = "smtp.gmail.com",
        smtp_port: int = 587,
        smtp_user: Optional[str] = None,
        smtp_app_password: Optional[str] = None,
        gmail_credentials_file: str = "credentials.json",
        gmail_token_file: str = "token.json",
        resume_path: Optional[str] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.storage = storage
        self.transport = (transport or "smtp").lower().strip()
        self.dry_run = dry_run
        self.daily_send_cap = daily_send_cap
        self.dedup_window_days = dedup_window_days
        self.min_jitter_seconds = min_jitter_seconds
        self.max_jitter_seconds = max_jitter_seconds
        self.sender_name = sender_name
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user or os.getenv("SMTP_USER", "")
        self.smtp_app_password = smtp_app_password or os.getenv(
            "SMTP_APP_PASSWORD", ""
        )
        self.sender_email = (
            sender_email
            or self.smtp_user
            or os.getenv("CANDIDATE_EMAIL", "alex.rivera@example.com")
        )
        self.gmail_credentials_file = gmail_credentials_file
        self.gmail_token_file = gmail_token_file
        self.resume_path = resume_path
        self._sleep_fn = sleep_fn or time.sleep

    def build_mime_message(self, draft: OutreachDraft) -> EmailMessage:
        """Builds a RFC-5322 multipart EmailMessage with optional PDF resume attachment."""
        msg = EmailMessage()
        msg["Subject"] = draft.subject
        msg["From"] = f"{self.sender_name} <{self.sender_email}>"
        msg["To"] = draft.recipient_email
        msg.set_content(draft.body_text)

        if draft.body_html:
            msg.add_alternative(draft.body_html, subtype="html")

        if self.resume_path:
            pdf_file = Path(self.resume_path)
            if pdf_file.is_file():
                pdf_bytes = pdf_file.read_bytes()
                msg.add_attachment(
                    pdf_bytes,
                    maintype="application",
                    subtype="pdf",
                    filename=pdf_file.name,
                )
        return msg

    def dispatch_single(
        self, draft: OutreachDraft, apply_jitter: bool = False
    ) -> DispatchOutcome:
        """Validates safety gates and dispatches (or dry-runs) a single OutreachDraft."""
        recipient = (draft.recipient_email or "").strip().lower()
        if not recipient or "@" not in recipient:
            return DispatchOutcome(
                job_id=draft.job_id,
                recipient_email=recipient,
                status="FAILED",
                transport=self.transport,
                reason="Invalid or empty recipient email address",
            )

        # Gate 1: 30-day recipient deduplication check
        if self.storage.was_contacted_recently(
            recipient, days=self.dedup_window_days
        ):
            self.storage.log_outreach(
                draft, transport=self.transport, status="SKIPPED_DUPLICATE"
            )
            return DispatchOutcome(
                job_id=draft.job_id,
                recipient_email=recipient,
                status="SKIPPED_DUPLICATE",
                transport=self.transport,
                reason=f"Recipient {recipient} was already contacted within the last {self.dedup_window_days} days",
            )

        # Gate 2: Daily send cap check
        sent_today = self.storage.get_daily_send_count()
        if sent_today >= self.daily_send_cap:
            self.storage.log_outreach(
                draft, transport=self.transport, status="SKIPPED_DAILY_CAP"
            )
            return DispatchOutcome(
                job_id=draft.job_id,
                recipient_email=recipient,
                status="SKIPPED_DAILY_CAP",
                transport=self.transport,
                reason=f"Daily send cap reached ({sent_today}/{self.daily_send_cap})",
            )

        # Gate 3: Dry-run mode (default safe posture)
        if self.dry_run:
            self.storage.log_outreach(
                draft, transport=f"{self.transport}:dry_run", status="DRY_RUN"
            )
            return DispatchOutcome(
                job_id=draft.job_id,
                recipient_email=recipient,
                status="DRY_RUN",
                transport=self.transport,
                reason="Dry-run mode active; email logged without live transmission",
            )

        # Optional human-like jitter delay between consecutive live sends
        if apply_jitter and self.max_jitter_seconds > 0:
            jitter = random.uniform(
                self.min_jitter_seconds,
                max(self.min_jitter_seconds, self.max_jitter_seconds),
            )
            logger.info("Sleeping %.1fs before sending next email...", jitter)
            self._sleep_fn(jitter)

        msg = self.build_mime_message(draft)

        try:
            if self.transport == "gmail_oauth":
                self._send_via_gmail_oauth(msg)
            else:
                self._send_via_smtp_tls(msg)

            self.storage.log_outreach(
                draft, transport=self.transport, status="SENT"
            )
            self.storage.update_job_status(draft.job_id, "CONTACTED")
            return DispatchOutcome(
                job_id=draft.job_id,
                recipient_email=recipient,
                status="SENT",
                transport=self.transport,
                reason=f"Dispatched successfully via {self.transport}",
            )
        except Exception as exc:
            logger.error("Failed to send email to %s: %s", recipient, exc)
            self.storage.log_outreach(
                draft, transport=self.transport, status="FAILED"
            )
            return DispatchOutcome(
                job_id=draft.job_id,
                recipient_email=recipient,
                status="FAILED",
                transport=self.transport,
                reason=str(exc),
            )

    def dispatch_batch(
        self, drafts: List[OutreachDraft]
    ) -> List[DispatchOutcome]:
        """Dispatches a batch of drafts sequentially with inter-email jitter."""
        outcomes: List[DispatchOutcome] = []
        live_sent_in_batch = 0

        for draft in drafts:
            outcome = self.dispatch_single(
                draft, apply_jitter=(live_sent_in_batch > 0 and not self.dry_run)
            )
            if outcome.status == "SENT":
                live_sent_in_batch += 1
            outcomes.append(outcome)

        return outcomes

    def _send_via_smtp_tls(self, msg: EmailMessage) -> None:
        """Sends an EmailMessage over SMTP TLS (smtp.gmail.com:587) using Gmail App Password."""
        if not self.smtp_user or not self.smtp_app_password:
            raise RuntimeError(
                "SMTP_USER and SMTP_APP_PASSWORD must be configured for live SMTP dispatch."
            )
        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(self.smtp_user, self.smtp_app_password)
            server.send_message(msg)

    def _send_via_gmail_oauth(self, msg: EmailMessage) -> None:
        """Sends an EmailMessage via the official Gmail v1 OAuth2 REST API."""
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError(
                "google-api-python-client and google-auth-oauthlib are required for gmail_oauth transport."
            ) from exc

        scopes = ["https://www.googleapis.com/auth/gmail.send"]
        creds = None
        token_path = Path(self.gmail_token_file)
        creds_path = Path(self.gmail_credentials_file)

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), scopes)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not creds_path.exists():
                    raise RuntimeError(
                        f"Gmail OAuth credentials file not found at '{creds_path}'."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(creds_path), scopes
                )
                creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json(), encoding="utf-8")

        service = build("gmail", "v1", credentials=creds)
        raw_encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        service.users().messages().send(
            userId="me", body={"raw": raw_encoded}
        ).execute()

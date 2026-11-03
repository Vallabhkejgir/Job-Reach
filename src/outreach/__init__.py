"""Outreach email composition and dual-transport (SMTP TLS + Gmail OAuth2) dispatch."""

from src.outreach.composer import OutreachComposer
from src.outreach.dispatcher import DispatchOutcome, OutreachDispatcher

__all__ = [
    "OutreachComposer",
    "OutreachDispatcher",
    "DispatchOutcome",
]

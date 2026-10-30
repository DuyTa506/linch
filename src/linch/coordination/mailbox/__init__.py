"""Peer-addressable mailbox substrate for multi-agent coordination."""

from __future__ import annotations

from .core import ClaimableMailbox, InMemoryMailbox, Mailbox, MailboxClaim, MailboxMessage
from .correlation import Correlator

__all__ = [
    "Correlator",
    "ClaimableMailbox",
    "InMemoryMailbox",
    "Mailbox",
    "MailboxClaim",
    "MailboxMessage",
]

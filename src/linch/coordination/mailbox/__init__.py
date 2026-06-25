"""Peer-addressable mailbox substrate for multi-agent coordination."""

from __future__ import annotations

from .core import InMemoryMailbox, Mailbox, MailboxMessage
from .correlation import Correlator
from .sqlite import SqliteMailbox

__all__ = [
    "Correlator",
    "InMemoryMailbox",
    "Mailbox",
    "MailboxMessage",
    "SqliteMailbox",
]

"""Optional mailbox coordination primitives.

Durable scheduling and queue dispatch live in :mod:`linch.worker`; this
package retains only the in-process mailbox capability.
"""

from __future__ import annotations

from .mailbox import Correlator, InMemoryMailbox, Mailbox, MailboxMessage
from .send_message import SendMessageTool

__all__ = [
    "Correlator",
    "InMemoryMailbox",
    "Mailbox",
    "MailboxMessage",
    "SendMessageTool",
]

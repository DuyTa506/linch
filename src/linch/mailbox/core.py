"""Peer-addressable mailbox substrate.

The mechanism under any multi-agent coordination pattern: a worker can address a
message to a *peer* (not just report up to its parent). The SDK ships only the
substrate — a :class:`Mailbox` protocol and an in-process default. Message
*semantics* (what a "plan", "shutdown", or "approval" means) are embedder
choreography, never core policy.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from uuid import uuid4


def _new_id() -> str:
    return uuid4().hex


@dataclass(slots=True)
class MailboxMessage:
    """One peer-to-peer message.

    ``sender``/``recipient`` are opaque addresses (a session id, a worker
    ``display_name``, or any embedder-chosen handle). ``type`` is a neutral
    category the embedder interprets. ``request_id``/``in_reply_to`` are the
    correlation hooks (see :class:`Correlator`): a request carries a
    ``request_id``; its response echoes that value in ``in_reply_to``.
    """

    sender: str
    recipient: str
    content: str
    type: str = "message"
    request_id: str | None = None
    in_reply_to: str | None = None
    id: str = field(default_factory=_new_id)


@runtime_checkable
class Mailbox(Protocol):
    """Duck-typed protocol for a peer-addressable message store.

    Implementations must make ``drain`` destructive and atomic so a message is
    delivered to exactly one drain, and concurrent ``send`` calls to one inbox
    never drop messages.
    """

    async def send(self, message: MailboxMessage) -> None: ...

    async def drain(self, recipient: str) -> list[MailboxMessage]: ...


class InMemoryMailbox:
    """In-process mailbox: per-recipient FIFO inboxes guarded by an async lock.

    The default backend. Suitable for multi-worker coordination within a single
    agent process; not durable across restarts (use a durable adapter for that).
    """

    def __init__(self) -> None:
        self._inboxes: dict[str, list[MailboxMessage]] = {}
        self._lock = asyncio.Lock()

    async def send(self, message: MailboxMessage) -> None:
        async with self._lock:
            self._inboxes.setdefault(message.recipient, []).append(message)

    async def drain(self, recipient: str) -> list[MailboxMessage]:
        async with self._lock:
            inbox = self._inboxes.get(recipient)
            if not inbox:
                return []
            drained = list(inbox)
            inbox.clear()
            return drained

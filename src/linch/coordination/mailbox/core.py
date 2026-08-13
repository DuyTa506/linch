"""Peer-addressable mailbox substrate.

The mechanism under any multi-agent coordination pattern: a worker can address a
message to a *peer* (not just report up to its parent). The SDK ships only the
substrate — a :class:`Mailbox` protocol and an in-process default. Message
*semantics* (what a "plan", "shutdown", or "approval" means) are embedder
choreography, never core policy.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Sequence
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


@dataclass(frozen=True, slots=True)
class MailboxClaim:
    """A mailbox message held under a renewable-by-reclaim delivery lease.

    Claims are capabilities: only the exact ``message.id``/``token`` pair may
    acknowledge or release the row.  Once ``expires_at`` has passed another
    consumer may reclaim the message and the old capability becomes stale.
    """

    message: MailboxMessage
    token: str
    owner: str
    expires_at: float


@runtime_checkable
class ClaimableMailbox(Mailbox, Protocol):
    """Optional leased-delivery extension for durable next-turn consumers."""

    async def claim(
        self,
        recipient: str,
        *,
        owner: str,
        lease_s: float,
        now: float | None = None,
        limit: int | None = None,
    ) -> list[MailboxClaim]: ...

    async def ack(self, claims: Sequence[MailboxClaim]) -> None: ...

    async def release(self, claims: Sequence[MailboxClaim]) -> None: ...


@dataclass(slots=True)
class _MailboxEntry:
    message: MailboxMessage
    lease_token: str | None = None
    lease_owner: str | None = None
    lease_expires_at: float | None = None


class InMemoryMailbox:
    """In-process mailbox: per-recipient FIFO inboxes guarded by an async lock.

    The default backend. Suitable for multi-worker coordination within a single
    agent process; not durable across restarts (use a durable adapter for that).
    """

    def __init__(self) -> None:
        self._inboxes: dict[str, list[_MailboxEntry]] = {}
        self._lock = asyncio.Lock()

    async def send(self, message: MailboxMessage) -> None:
        async with self._lock:
            self._inboxes.setdefault(message.recipient, []).append(_MailboxEntry(message=message))

    async def drain(self, recipient: str) -> list[MailboxMessage]:
        async with self._lock:
            inbox = self._inboxes.get(recipient)
            if not inbox:
                return []
            drained = [entry.message for entry in inbox]
            # Drop the now-empty bucket so the dict doesn't grow unbounded with
            # one stale entry per recipient ever addressed.
            del self._inboxes[recipient]
            return drained

    async def claim(
        self,
        recipient: str,
        *,
        owner: str,
        lease_s: float,
        now: float | None = None,
        limit: int | None = None,
    ) -> list[MailboxClaim]:
        claimed_at = time.time() if now is None else now
        _validate_claim_args(owner=owner, lease_s=lease_s, now=claimed_at, limit=limit)
        if limit == 0:
            return []

        async with self._lock:
            inbox = self._inboxes.get(recipient, ())
            claims: list[MailboxClaim] = []
            for entry in inbox:
                # Strict FIFO: a still-valid lease on the oldest undelivered
                # row blocks this consumer from skipping ahead to newer rows.
                if (
                    entry.lease_token is not None
                    and entry.lease_expires_at is not None
                    and entry.lease_expires_at > claimed_at
                ):
                    break
                token = _new_id()
                expires_at = claimed_at + lease_s
                entry.lease_token = token
                entry.lease_owner = owner
                entry.lease_expires_at = expires_at
                claims.append(
                    MailboxClaim(
                        message=entry.message,
                        token=token,
                        owner=owner,
                        expires_at=expires_at,
                    )
                )
                if limit is not None and len(claims) >= limit:
                    break
            return claims

    async def ack(self, claims: Sequence[MailboxClaim]) -> None:
        if not claims:
            return
        capabilities = {(claim.message.id, claim.token) for claim in claims}
        async with self._lock:
            for recipient, inbox in list(self._inboxes.items()):
                remaining = [
                    entry
                    for entry in inbox
                    if (entry.message.id, entry.lease_token or "") not in capabilities
                ]
                if remaining:
                    self._inboxes[recipient] = remaining
                else:
                    del self._inboxes[recipient]

    async def release(self, claims: Sequence[MailboxClaim]) -> None:
        if not claims:
            return
        capabilities = {(claim.message.id, claim.token) for claim in claims}
        async with self._lock:
            for inbox in self._inboxes.values():
                for entry in inbox:
                    if (entry.message.id, entry.lease_token or "") in capabilities:
                        entry.lease_token = None
                        entry.lease_owner = None
                        entry.lease_expires_at = None


def _validate_claim_args(*, owner: str, lease_s: float, now: float, limit: int | None) -> None:
    if not isinstance(owner, str) or not owner:
        raise ValueError("owner must be non-empty")
    if isinstance(lease_s, bool) or not isinstance(lease_s, (int, float)):
        raise ValueError("lease_s must be a positive finite number")
    if not math.isfinite(lease_s) or lease_s <= 0:
        raise ValueError("lease_s must be a positive finite number")
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError("now must be finite")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise ValueError("limit must be a non-negative integer or None")

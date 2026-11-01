"""Peer-addressable mailbox substrate.

The mechanism under any multi-agent coordination pattern: a worker can address a
message to a *peer* (not just report up to its parent). The SDK ships only the
substrate — a :class:`Mailbox` protocol and an in-process default. Message
*semantics* (what a "plan", "shutdown", or "approval" means) are embedder
choreography, never core policy.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from uuid import uuid4

from .._claim_validation import _validate_claim_args


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

    Args:
        sender: Opaque address of the message producer.
        recipient: Opaque address of the destination inbox.
        content: Message body delivered to the peer.
        type: Neutral message category interpreted by the embedder.
        request_id: Optional identifier for a request awaiting a response.
        in_reply_to: Optional request identifier being answered.
        id: Stable message identifier; generated when omitted.

    Attributes:
        request_id: Correlates a request with a later response without imposing
            semantics on the mailbox transport.
        in_reply_to: Echoes the request identifier on a response message.
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

    async def send(self, message: MailboxMessage) -> None:
        """Enqueue a message before returning.

        Implementations must not report success before the message is safely
        accepted by their delivery store. In-memory implementations provide
        process-local delivery only; durable adapters must commit their write.
        There is no deduplication guarantee beyond the message identity supplied
        by the caller.

        Args:
            message: Message to append to the recipient's FIFO inbox.
        """
        ...

    async def drain(self, recipient: str) -> list[MailboxMessage]:
        """Atomically remove and return a recipient's pending messages.

        A destructive drain is a separate delivery mode from lease-based
        ``claim``. Callers must not mix the two modes for one recipient; this
        protocol does not require a drain to preserve active claims. Durable
        adapters must commit the removals before returning.

        Args:
            recipient: Inbox address to drain.

        Returns:
            Messages in FIFO order; each is delivered to at most one drain.
        """
        ...


@dataclass(frozen=True, slots=True)
class MailboxClaim:
    """A mailbox message held under a renewable-by-reclaim delivery lease.

    Claims are capabilities: only the exact ``message.id``/``token`` pair may
    acknowledge or release the row.  Once ``expires_at`` has passed another
    consumer may reclaim the message and the old capability becomes stale.

    Attributes:
        token: Capability checked together with ``message.id`` by ack/release.
        owner: Informational consumer identity carried with the lease.
        expires_at: Absolute timestamp in seconds after which the row is reclaimable.

    Args:
        message: Message held by this claim.
        token: Capability token required for acknowledgement or release.
        owner: Consumer identity that acquired the claim.
        expires_at: Absolute expiry time after which reclamation is allowed.
    """

    message: MailboxMessage
    token: str
    owner: str
    expires_at: float


@runtime_checkable
class ClaimableMailbox(Mailbox, Protocol):
    """Define leased delivery for durable next-turn consumers.

    ``claim`` is strict FIFO: a live lease on the oldest row blocks later rows
    until it expires. ``ack`` and ``release`` use the exact message/token pair;
    stale or already-completed claims are harmless no-ops. Durable adapters
    must commit each state transition before returning.
    """

    async def claim(
        self,
        recipient: str,
        *,
        owner: str,
        lease_s: float,
        now: float | None = None,
        limit: int | None = None,
    ) -> list[MailboxClaim]:
        """Claim available messages for one recipient.

        The returned prefix is claimed atomically under the mailbox's ordering
        rules; a live lease on the oldest row prevents skipping ahead. Durable
        adapters commit the lease assignments before returning.

        Args:
            recipient: Inbox address to claim from.
            owner: Consumer identity recorded on each lease.
            lease_s: Lease duration in seconds.
            now: Optional clock value used for deterministic expiry checks.
            limit: Optional maximum number of claims; zero returns no claims.

        Returns:
            FIFO claims up to ``limit``, stopping before the first live lease.
        """
        ...

    async def ack(self, claims: Sequence[MailboxClaim]) -> None:
        """Acknowledge and remove claimed messages after delivery is durable.

        Stale or already-removed claims are ignored; durable adapters commit
        the removals before returning.

        Args:
            claims: Exact claim capabilities being acknowledged.
        """
        ...

    async def release(self, claims: Sequence[MailboxClaim]) -> None:
        """Release claimed messages for retry or later reclamation.

        Stale claims are ignored; durable adapters commit lease removal before
        returning.

        Args:
            claims: Exact claim capabilities being released.
        """
        ...


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
        """Append a message to the process-local FIFO inbox.

        Args:
            message: Message to append for its recipient.
        """
        async with self._lock:
            self._inboxes.setdefault(message.recipient, []).append(_MailboxEntry(message=message))

    async def drain(self, recipient: str) -> list[MailboxMessage]:
        """Destructively drain one process-local inbox in FIFO order.

        This implementation removes the whole bucket, including entries with
        active claim metadata; use either drain or lease delivery for a given
        recipient, never both. The process-local mutation needs no external
        durability commit.

        Args:
            recipient: Inbox address to remove.

        Returns:
            All messages currently present for the recipient.
        """
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
        """Claim process-local messages using strict FIFO leases.

        Args:
            recipient: Inbox address to claim from.
            owner: Consumer identity recorded on each lease.
            lease_s: Lease duration in seconds.
            now: Optional clock value used for deterministic expiry checks.
            limit: Optional maximum number of claims.

        Returns:
            Claims up to ``limit``; a live oldest lease blocks later rows.
        """
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
        """Remove matching process-local claims after delivery succeeds.

        Args:
            claims: Exact message/token capabilities to acknowledge.

        Stale capabilities are ignored.
        """
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
        """Clear matching process-local leases so messages can be retried.

        Args:
            claims: Exact message/token capabilities to release.

        Stale capabilities are ignored.
        """
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

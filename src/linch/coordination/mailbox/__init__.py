from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from functools import partial
from typing import Any, cast
from xml.sax.saxutils import escape

from ..._telemetry import store_call
from ...errors import ConfigError
from ...events import Event, UserEvent
from ...kernel import Context, Disposable
from ...types import Message, TextBlock
from .core import ClaimableMailbox, InMemoryMailbox, Mailbox, MailboxClaim, MailboxMessage
from .correlation import Correlator


class MailboxPlugin:
    """Provide mailbox service, inbox delivery, and the send-message tool."""

    name = "mailbox"
    version = "1"
    provides: Iterable[str] = ("mailbox",)
    requires: Iterable[str] = ("agent",)

    def setup(self, ctx: Context, config: Any = None) -> Disposable:
        """Install mailbox-backed messaging for one agent activation.

        Args:
            ctx: Plugin activation context containing the owning agent.
            config: Mailbox implementation used for delivery and sending.

        Returns:
            A disposer for plugin-specific cleanup; context effects own the
            service, inbox producer, and optional tool registration.
        """
        agent = ctx.agent
        ctx.register("mailbox", config)
        ctx.inbox(partial(_mailbox_producer, config))
        if agent.tools.get("send_message") is None:
            from ..send_message import SendMessageTool

            ctx.effect(lambda: agent.tools.register(SendMessageTool(config, agent._sessions.get)))
        return Disposable()


async def _mailbox_producer(
    mailbox: Any, session: Any, _run_id: str
) -> AsyncIterator[Message | Event]:
    address = getattr(session, "mailbox_address", None)
    if not address:
        return
    durability: Any = getattr(session.agent, "durability", None)
    if not getattr(durability, "durable_inbox", False):
        for message in await mailbox.drain(address):
            yield render_peer_message(message)
        return

    claim = getattr(mailbox, "claim", None)
    ack = getattr(mailbox, "ack", None)
    release = getattr(mailbox, "release", None)
    if not all(callable(method) for method in (claim, ack, release)):
        raise ConfigError(
            "durable_inbox with mailbox requires ClaimableMailbox claim(), ack(), and release()"
        )
    claims = await cast(Any, claim)(
        address,
        owner=f"session:{session.id}",
        lease_s=durability.delivery_lease_s,
    )
    delivered = 0
    try:
        for claimed in claims:
            message = claimed.message
            await store_call(
                session,
                "session.enqueue_inbox",
                session.notify(
                    render_peer_message(message),
                    delivery_id=f"mailbox:{message.id}",
                    source="mailbox",
                    metadata={"message_id": message.id, "recipient": message.recipient},
                ),
            )
            await cast(Any, ack)([claimed])
            delivered += 1
    except BaseException:
        pending = claims[delivered:]
        if pending:
            await cast(Any, release)(pending)
        raise

    stored = await store_call(
        session,
        "session.commit_inbox",
        session.store.commit_inbox(session.id, after_seq=session._last_seq),
    )
    session._track_seqs(stored)
    for row in stored:
        session.session_log.append(row.message)
        yield UserEvent(message=row.message, subtype="notification")


def render_peer_message(message: Any) -> Message:
    parts = [
        "<peer-message>",
        f"<from>{escape(message.sender)}</from>",
        f"<type>{escape(message.type)}</type>",
    ]
    if message.request_id:
        parts.append(f"<request-id>{escape(message.request_id)}</request-id>")
    if message.in_reply_to:
        parts.append(f"<in-reply-to>{escape(message.in_reply_to)}</in-reply-to>")
    parts.append(f"<content>{escape(message.content)}</content></peer-message>")
    return Message(role="user", content=[TextBlock(text="".join(parts))])


__all__ = [
    "MailboxPlugin",
    "Correlator",
    "ClaimableMailbox",
    "InMemoryMailbox",
    "Mailbox",
    "MailboxClaim",
    "MailboxMessage",
]

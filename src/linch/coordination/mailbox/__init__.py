from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from functools import partial
from typing import Any, cast
from xml.sax.saxutils import escape

from ... import lifecycle
from ...errors import ConfigError
from ...events import Event, UserEvent
from ...hooks import HookDispatcher, PostToolUseContext
from ...kernel import Context, Disposable
from ...profiles import _base_run_config, _effective_run_config
from ...run_store import durable_call
from ...tools import ToolResult
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
            config: Mailbox implementation used for delivery and sending. It
                must be provided; durable inbox mode additionally requires the
                ``ClaimableMailbox`` methods ``claim``, ``ack``, and ``release``.

        Returns:
            A disposer for plugin-specific cleanup; context effects own the
            service, inbox producer, and optional tool registration.
        """
        agent = ctx.agent
        ctx.register("mailbox", config)
        ctx.effect(lambda: ctx.run_config.inbox(partial(_mailbox_producer, config)))
        if ctx.run_config.tools.get("send_message") is None:
            from ..send_message import SendMessageTool

            ctx.effect(
                lambda: ctx.run_config.tools.register(SendMessageTool(config, agent._sessions.get))
            )
        return Disposable()


async def _mailbox_producer(
    mailbox: Any, session: Any, _run_id: str
) -> AsyncIterator[Message | Event]:
    address = getattr(session, "mailbox_address", None)
    if not address:
        return
    dispatcher = _mailbox_hook_dispatcher(session, _run_id)
    durability: Any = getattr(session.agent, "durability", None)
    if not getattr(durability, "durable_inbox", False):
        messages, drain_error = await _drain_batch(mailbox, address)
        if drain_error is not None:
            if messages:
                await _redeliver(mailbox, messages)
            raise drain_error
        if not messages:
            return
        appended = False
        try:
            rendered: list[Message] = []
            hook_events: list[Event] = []
            for message in messages:
                peer_message, events = await _render_peer_message(
                    message, session=session, run_id=_run_id, dispatcher=dispatcher
                )
                rendered.append(peer_message)
                hook_events.extend(events)
            appended, append_error = await _append_batch(session, rendered)
            if append_error is not None:
                raise append_error
        except BaseException:
            if not appended:
                await _redeliver(mailbox, messages)
            raise
        for event in hook_events:
            yield event
        for message in rendered:
            yield UserEvent(message=message, subtype="notification")
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
    if not claims:
        return
    hook_events: list[Event] = []
    acked = False
    try:
        for claimed in claims:
            message = claimed.message
            peer_message, events = await _render_peer_message(
                message, session=session, run_id=_run_id, dispatcher=dispatcher
            )
            hook_events.extend(events)
            await session.notify(
                peer_message,
                delivery_id=f"mailbox:{message.id}",
                source="mailbox",
                metadata={"message_id": message.id, "recipient": message.recipient},
            )
        stored = await durable_call(
            session,
            "session.commit_inbox",
            session.store.commit_inbox(session.id, after_seq=session._last_seq),
        )
        if stored:
            session.session_log.append_many([row.message for row in stored])
            session._track_seqs(stored)
        await cast(Any, ack)(claims)
        acked = True
    except BaseException:
        if not acked:
            await cast(Any, release)(claims)
        raise

    for event in hook_events:
        yield event
    for row in stored:
        yield UserEvent(message=row.message, subtype="notification")


async def _append_batch(session: Any, messages: list[Message]) -> tuple[bool, BaseException | None]:
    """Append a batch and reconcile cancellation with the append outcome."""
    task = asyncio.create_task(session.append(messages))
    try:
        await asyncio.shield(task)
    except BaseException as exc:
        while not task.done():
            try:
                await asyncio.shield(task)
            except BaseException:
                continue
        if task.cancelled():
            return False, exc
        error = task.exception()
        if error is not None:
            return False, error
        return True, exc
    return True, None


async def _drain_batch(
    mailbox: Any, address: str
) -> tuple[list[MailboxMessage], BaseException | None]:
    """Drain a batch under a shield so cancellation can reconcile its result."""
    task = asyncio.create_task(mailbox.drain(address))
    try:
        messages = await asyncio.shield(task)
    except BaseException as exc:
        while not task.done():
            try:
                await asyncio.shield(task)
            except BaseException:
                continue
        if task.cancelled():
            return [], exc
        error = task.exception()
        if error is not None:
            return [], error
        return cast(list[MailboxMessage], task.result()), exc
    return cast(list[MailboxMessage], messages), None


async def _redeliver(mailbox: Any, messages: list[MailboxMessage]) -> None:
    """Return a destructively drained batch to its source."""
    first_error: BaseException | None = None
    for message in messages:
        task = asyncio.create_task(mailbox.send(message))
        try:
            await asyncio.shield(task)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
            while not task.done():
                try:
                    await asyncio.shield(task)
                except BaseException:
                    continue
            error = task.exception() if not task.cancelled() else None
            if error is not None and first_error is exc:
                first_error = error
    if first_error is not None:
        raise first_error


def _mailbox_hook_dispatcher(session: Any, run_id: str) -> HookDispatcher:
    """Resolve PostToolUse hooks from the active run configuration."""
    try:
        config = _effective_run_config(session, run_id)
    except (AttributeError, ConfigError):
        config = _base_run_config(session)
    return HookDispatcher(config.hooks)


async def _render_peer_message(
    message: MailboxMessage,
    *,
    session: Any,
    run_id: str,
    dispatcher: HookDispatcher,
) -> tuple[Message, list[Event]]:
    """Render one peer message through the active PostToolUse hook chain."""
    rendered = render_peer_message(message)
    if not dispatcher.active:
        return rendered, []
    text = "".join(getattr(block, "text", "") for block in rendered.content)
    dispatched = await dispatcher.dispatch(
        lifecycle.POST_TOOL_USE,
        PostToolUseContext(
            session=session,
            run_id=run_id,
            turn_index=None,
            deps=getattr(session, "run_deps", None),
            tool_use_id=f"mailbox:{message.id}",
            tool_name="mailbox",
            input={},
            result=ToolResult(content=text),
        ),
    )
    result = getattr(dispatched.context, "result", None)
    if isinstance(result, ToolResult):
        rendered = Message(role="user", content=[TextBlock(text=result.content)])
    return rendered, list(dispatched.events)


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

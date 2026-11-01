"""Regression tests for next-turn notification and mailbox delivery."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn


def _text(message: Any) -> str:
    return "".join(getattr(block, "text", "") for block in message.content)


@pytest.fixture(params=["memory", "postgres"], ids=["memory", "postgres"])
async def delivery_store(request: Any, pg_dsn: str) -> Any:
    """Provide the session-store backends used by delivery regressions."""
    from linch.sessions import InMemorySessionStore
    from linch.sessions.postgres import PostgresSessionStore

    store = InMemorySessionStore() if request.param == "memory" else PostgresSessionStore(pg_dsn)
    try:
        yield store
    finally:
        await store.close()


class _FlakyNotificationStore:
    """Fail the first durable append containing a selected message marker."""

    def __init__(self, marker: str = "task-notification") -> None:
        from linch.sessions import InMemorySessionStore

        self._inner = InMemorySessionStore()
        self.marker = marker
        self.fail_next = False

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def append_messages(self, session_id: str, messages: list[Any]) -> Any:
        if self.fail_next and any(self.marker in str(message.content) for message in messages):
            self.fail_next = False
            raise ConnectionError("transient store outage")
        return await self._inner.append_messages(session_id, messages)


class _AppendBarrierStore:
    """Block an append after the backend has stored its rows."""

    def __init__(self) -> None:
        from linch.sessions import InMemorySessionStore

        self._inner = InMemorySessionStore()
        self.appended = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def append_messages(self, session_id: str, messages: list[Any]) -> Any:
        stored = await self._inner.append_messages(session_id, messages)
        if any("peer-message" in str(message.content) for message in messages):
            self.appended.set()
            await self.release.wait()
        return stored


class _DrainBarrierMailbox:
    """Remove a batch, then pause before returning it to the producer."""

    def __init__(self) -> None:
        from linch import InMemoryMailbox

        self._inner = InMemoryMailbox()
        self.removed = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, message: Any) -> None:
        await self._inner.send(message)

    async def drain(self, recipient: str) -> list[Any]:
        messages = await self._inner.drain(recipient)
        self.removed.set()
        await self.release.wait()
        return messages


async def test_notification_survives_failed_append() -> None:
    """Keep an in-process notify delivery queued until its append succeeds."""

    from linch.types import Message, TextBlock

    store = _FlakyNotificationStore()
    agent = make_agent(
        ScriptedProvider([TextTurn("a"), TextTurn("b")]),
        session_store=store,
        run_store=None,
    )
    session = await agent.session()
    await session.notify(
        Message(role="user", content=[TextBlock(text="<task-notification>w1 done")])
    )

    store.fail_next = True
    [event async for event in session.run("first")]
    [event async for event in session.run("second")]

    texts = [_text(message) for message in session.full_history]
    assert "<task-notification>w1 done" in texts
    await agent.close()


async def test_non_durable_mailbox_redelivers_after_append_failure() -> None:
    """Return a drained mailbox batch when its session append fails."""

    from linch import InMemoryMailbox, MailboxMessage, MailboxPlugin

    store = _FlakyNotificationStore(marker="peer-message")
    box = InMemoryMailbox()
    agent = make_agent(
        ScriptedProvider([TextTurn("a"), TextTurn("b")]),
        session_store=store,
        run_store=None,
    )
    await agent.use(MailboxPlugin(), box)
    session = await agent.session()
    session.mailbox_address = "alice"
    await box.send(MailboxMessage(sender="bob", recipient="alice", content="retry-me"))

    store.fail_next = True
    [event async for event in session.run("first")]
    leftover = await box.drain("alice")
    assert len(leftover) == 1
    for message in leftover:
        await box.send(message)

    [event async for event in session.run("second")]
    assert len(await box.drain("alice")) == 0
    assert sum("retry-me" in _text(message) for message in session.provider_view) == 1
    await agent.close()


async def test_non_durable_mailbox_cancellation_after_store_append_has_no_duplicate() -> None:
    """Finish a canceled append before deciding whether to redeliver the source row."""

    from linch import InMemoryMailbox, MailboxMessage, MailboxPlugin

    store = _AppendBarrierStore()
    box = InMemoryMailbox()
    agent = make_agent(
        ScriptedProvider([TextTurn("a"), TextTurn("b")]),
        session_store=store,
        run_store=None,
    )
    await agent.use(MailboxPlugin(), box)
    session = await agent.session()
    session.mailbox_address = "alice"
    await box.send(MailboxMessage(sender="bob", recipient="alice", content="cancel-after-store"))

    run = cast(Any, session.run("go"))
    events: list[Any] = []

    async def consume() -> None:
        async for event in run:
            events.append(event)

    consumer = asyncio.create_task(consume())
    await store.appended.wait()
    consumer.cancel()
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await run.aclose()

    [event async for event in session.run("again")]
    assert len(await box.drain("alice")) == 0
    assert sum("cancel-after-store" in _text(message) for message in session.full_history) == 1
    stored = [row.message for row in await session.store.load_messages(session.id)]
    assert sum("cancel-after-store" in _text(message) for message in stored) == 1
    await agent.close()


async def test_non_durable_mailbox_cancellation_during_render_redelivers() -> None:
    """Return a destructively drained batch when rendering is canceled."""

    from linch import InMemoryMailbox, MailboxMessage, MailboxPlugin

    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingPostToolHook:
        name = "blocking-mailbox-render"

        async def on_post_tool_use(self, _context: Any) -> None:
            started.set()
            await release.wait()

    box = InMemoryMailbox()
    agent = make_agent(
        ScriptedProvider([TextTurn("a"), TextTurn("b")]),
        run_store=None,
        hooks=BlockingPostToolHook(),
    )
    await agent.use(MailboxPlugin(), box)
    session = await agent.session()
    session.mailbox_address = "alice"
    await box.send(MailboxMessage(sender="bob", recipient="alice", content="cancel-render"))

    run = cast(Any, session.run("go"))
    events: list[Any] = []

    async def consume() -> None:
        async for event in run:
            events.append(event)

    consumer = asyncio.create_task(consume())
    await started.wait()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await run.aclose()
    release.set()

    leftover = await box.drain("alice")
    assert len(leftover) == 1
    for message in leftover:
        await box.send(message)
    [event async for event in session.run("again")]
    assert sum("cancel-render" in _text(message) for message in session.provider_view) == 1
    await agent.close()


async def test_non_durable_mailbox_cancellation_after_drain_requeues() -> None:
    """Reconcile a cancellation after a destructive drain has removed its batch."""

    from linch import MailboxMessage, MailboxPlugin

    box = _DrainBarrierMailbox()
    agent = make_agent(
        ScriptedProvider([TextTurn("a"), TextTurn("b")]),
        run_store=None,
    )
    await agent.use(MailboxPlugin(), box)
    session = await agent.session()
    session.mailbox_address = "alice"
    await box.send(MailboxMessage(sender="bob", recipient="alice", content="cancel-drain"))

    run = cast(Any, session.run("go"))
    events: list[Any] = []

    async def consume() -> None:
        async for event in run:
            events.append(event)

    consumer = asyncio.create_task(consume())
    await box.removed.wait()
    consumer.cancel()
    box.release.set()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await run.aclose()

    leftover = await box._inner.drain("alice")
    assert len(leftover) == 1
    for message in leftover:
        await box.send(message)
    [event async for event in session.run("again")]
    assert sum("cancel-drain" in _text(message) for message in session.provider_view) == 1
    await agent.close()


async def test_non_durable_mailbox_render_failure_redelivers() -> None:
    """Return a drained message when peer rendering rejects malformed input."""

    from linch import InMemoryMailbox, MailboxMessage, MailboxPlugin

    box = InMemoryMailbox()
    agent = make_agent(ScriptedProvider([TextTurn("a")]), run_store=None)
    await agent.use(MailboxPlugin(), box)
    session = await agent.session()
    session.mailbox_address = "alice"
    await box.send(
        MailboxMessage(
            sender=cast(Any, None),
            recipient="alice",
            content="render-failure",  # type: ignore[arg-type]
        )
    )

    events = [event async for event in session.run("go")]
    assert any(getattr(event, "type", None) == "error" for event in events)
    assert len(await box.drain("alice")) == 1
    await agent.close()


async def test_closing_run_mid_mailbox_delivery_preserves_pending_peer_messages(
    delivery_store: Any,
) -> None:
    """Leave unappended non-durable mailbox messages available for the next turn."""

    from linch import InMemoryMailbox, MailboxMessage, MailboxPlugin

    box = InMemoryMailbox()
    agent = make_agent(
        ScriptedProvider([TextTurn("a"), TextTurn("b")]),
        session_store=delivery_store,
        run_store=None,
    )
    await agent.use(MailboxPlugin(), box)
    session = await agent.session()
    session.mailbox_address = "alice"
    for index in range(3):
        await box.send(MailboxMessage(sender="bob", recipient="alice", content=f"msg-{index}"))

    run = cast(Any, session.run("go"))
    async for event in run:
        if (
            getattr(event, "type", None) == "user"
            and getattr(event, "subtype", None) == "notification"
        ):
            break
    await run.aclose()
    [event async for event in session.run("again")]

    seen = sum(
        any(f"msg-{index}" in _text(message) for index in range(3))
        for message in session.provider_view
    )
    leftover = await box.drain("alice")
    assert seen + len(leftover) == 3
    await agent.close()


async def test_closing_run_mid_durable_mailbox_delivery_keeps_live_log_in_sync(
    delivery_store: Any,
) -> None:
    """Expose every committed durable mailbox row in the live session log."""

    from linch import InMemoryMailbox, MailboxMessage, MailboxPlugin
    from linch.durability import DurabilityOptions

    box = InMemoryMailbox()
    agent = make_agent(
        ScriptedProvider([TextTurn("a"), TextTurn("b")]),
        session_store=delivery_store,
        run_store=None,
        durability=DurabilityOptions(durable_inbox=True),
    )
    await agent.use(MailboxPlugin(), box)
    session = await agent.session()
    session.mailbox_address = "alice"
    for index in range(3):
        await box.send(MailboxMessage(sender="bob", recipient="alice", content=f"msg-{index}"))

    run = cast(Any, session.run("go"))
    async for event in run:
        if (
            getattr(event, "type", None) == "user"
            and getattr(event, "subtype", None) == "notification"
        ):
            break
    await run.aclose()
    [event async for event in session.run("again")]

    stored = [row.message for row in await session.store.load_messages(session.id)]
    in_store = sum(
        any(f"msg-{index}" in _text(message) for index in range(3)) for message in stored
    )
    in_view = sum(
        any(f"msg-{index}" in _text(message) for index in range(3))
        for message in session.provider_view
    )
    assert in_store == 3
    assert in_view == in_store
    await agent.close()


async def test_mailbox_delivery_uses_active_profile_redaction_hook(delivery_store: Any) -> None:
    """Apply PostToolUse redaction policy from the active run configuration."""

    from linch import (
        Disposable,
        InMemoryMailbox,
        MailboxMessage,
        MailboxPlugin,
        Profile,
        RedactionConfig,
        RedactionHook,
        RedactionRule,
        RunOptions,
    )

    class RedactionPlugin:
        name = "profile-redaction"
        version = "1"
        provides = ()
        requires = ("run_config",)

        def setup(self, ctx: Any, config: Any = None) -> Disposable:
            hook = RedactionHook(RedactionConfig(rules=(RedactionRule("SECRET", "[REDACTED]"),)))
            hooks = ctx.run_config.hooks
            hooks.append(hook)
            return Disposable(lambda: hooks.remove(hook))

    from tests.support.providers import ScriptedProvider

    box = InMemoryMailbox()
    agent = make_agent(
        ScriptedProvider([TextTurn("done")]),
        session_store=delivery_store,
        run_store=None,
    )
    await agent.use(MailboxPlugin(), box)
    session = await agent.session()
    session.mailbox_address = "alice"
    await box.send(MailboxMessage(sender="bob", recipient="alice", content="SECRET"))

    profile = Profile("redaction", "1", plugins={"redaction": (cast(Any, RedactionPlugin()), {})})
    [event async for event in session.run("go", RunOptions(profile=profile))]

    mailbox_messages = [
        message for message in session.provider_view if "peer-message" in _text(message)
    ]
    assert len(mailbox_messages) == 1
    assert "[REDACTED]" in _text(mailbox_messages[0])
    assert "SECRET" not in _text(mailbox_messages[0])
    await agent.close()

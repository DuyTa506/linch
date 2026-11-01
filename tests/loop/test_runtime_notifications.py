"""Notification delivery keeps queued, durable, and live history consistent."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn
from tests.support.stores import memory_session_store


@pytest.fixture(params=["memory", "postgres"])
async def inbox_store(request: Any, pg_dsn: str) -> AsyncIterator[Any]:
    from linch.sessions.postgres import PostgresSessionStore

    store = memory_session_store() if request.param == "memory" else PostgresSessionStore(pg_dsn)
    try:
        yield store
    finally:
        await store.close()


class _NotificationAppendStore:
    """Fail or pause notification writes around the real backend commit."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.fail_next = False
        self.pause = False
        self.after_commit = False
        self.commit_error: Exception | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def append_messages(self, session_id: str, messages: list[Any]) -> Any:
        if not any("task-notification" in str(message.content) for message in messages):
            return await self.inner.append_messages(session_id, messages)
        if self.pause:
            if self.after_commit:
                stored = await self.inner.append_messages(session_id, messages)
            self.entered.set()
            await self.release.wait()
            if self.after_commit:
                return stored
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("notification append failed")
        return await self.inner.append_messages(session_id, messages)

    async def commit_inbox(self, session_id: str, *, after_seq: int) -> Any:
        stored = await self.inner.commit_inbox(session_id, after_seq=after_seq)
        if self.pause:
            self.entered.set()
            await self.release.wait()
        if self.commit_error is not None:
            raise self.commit_error
        return stored


async def test_failed_notification_append_is_retried_next_run(inbox_store: Any) -> None:
    """Clearing the queue before append must not lose a next-turn notification."""
    from linch.types import Message, TextBlock

    store = _NotificationAppendStore(inbox_store)
    provider = ScriptedProvider([TextTurn("done")])
    agent = make_agent(provider, session_store=store, run_store=None)
    try:
        session = await agent.session()
        await session.notify(Message(role="user", content=[TextBlock(text="task-notification")]))
        store.fail_next = True
        [event async for event in session.run("first")]
        [event async for event in session.run("retry")]

        assert sum("task-notification" in str(m.content) for m in session.full_history) == 1
        assert "task-notification" in str(provider.requests[0].messages)
        assert session.pending_notifications == []
    finally:
        await agent.close()


@pytest.mark.parametrize("durable", [False, True], ids=["queued", "durable"])
async def test_notification_batch_survives_close_after_first_event(
    inbox_store: Any, durable: bool
) -> None:
    """Stopping delivery must preserve every committed notification for the next model call."""
    from linch import DurabilityOptions
    from linch.loop.runtime import drain_pending_notifications
    from linch.types import Message, TextBlock

    provider = ScriptedProvider([TextTurn("done")])
    agent = make_agent(
        provider,
        session_store=inbox_store,
        run_store=None,
        durability=DurabilityOptions(durable_inbox=durable),
    )
    try:
        session = await agent.session()
        for text in ("first-note", "second-note", "third-note"):
            await session.notify(Message(role="user", content=[TextBlock(text=text)]))
        drain = drain_pending_notifications(session, "delivery")
        try:
            event = await anext(drain)
            assert "first-note" in str(event)
        finally:
            await drain.aclose()

        stored = [row.message for row in await inbox_store.load_messages(session.id)]
        assert list(session.full_history) == stored
        [event async for event in session.run("next turn")]
        for text in ("first-note", "second-note", "third-note"):
            assert sum(text in str(m.content) for m in provider.requests[0].messages) == 1
        assert list(session.full_history) == [
            row.message for row in await inbox_store.load_messages(session.id)
        ]
    finally:
        await agent.close()


@pytest.mark.parametrize(
    ("after_commit", "fail_append"),
    [(False, False), (False, True), (True, False)],
    ids=["before-commit", "failed-append", "after-commit"],
)
async def test_notification_append_reconciles_repeated_cancellation(
    after_commit: bool, fail_append: bool
) -> None:
    """Cancellation must neither discard uncommitted notes nor duplicate committed notes."""
    from linch.loop.runtime import drain_pending_notifications
    from linch.types import Message, TextBlock

    store = _NotificationAppendStore(memory_session_store())
    agent = make_agent(ScriptedProvider([]), session_store=store, run_store=None)
    try:
        session = await agent.session()
        await session.notify(Message(role="user", content=[TextBlock(text="task-notification")]))
        store.pause = True
        store.after_commit = after_commit
        store.fail_next = fail_append
        drain = drain_pending_notifications(session, "delivery")
        task = asyncio.create_task(anext(drain))
        await asyncio.wait_for(store.entered.wait(), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        store.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await drain.aclose()

        store.pause = False
        [event async for event in drain_pending_notifications(session, "retry")]
        rows = await store.load_messages(session.id)
        assert sum("task-notification" in str(row.message.content) for row in rows) == 1
        assert list(session.full_history) == [row.message for row in rows]
        assert session.pending_notifications == []
    finally:
        await agent.close()


async def test_cancelled_inbox_commit_remains_visible_next_run(inbox_store: Any) -> None:
    """A cancelled commit response cannot let the next prompt skip committed inbox rows."""
    from linch import DurabilityOptions
    from linch.loop.runtime import drain_pending_notifications
    from linch.types import Message, TextBlock

    store = _NotificationAppendStore(inbox_store)
    provider = ScriptedProvider([TextTurn("done")])
    agent = make_agent(
        provider,
        session_store=store,
        run_store=None,
        durability=DurabilityOptions(durable_inbox=True),
    )
    try:
        session = await agent.session()
        await session.notify(Message(role="user", content=[TextBlock(text="durable-note")]))
        store.pause = True
        drain = drain_pending_notifications(session, "delivery")
        task = asyncio.create_task(anext(drain))
        await asyncio.wait_for(store.entered.wait(), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        store.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await drain.aclose()

        store.pause = False
        [event async for event in session.run("next turn")]
        assert sum("durable-note" in str(m.content) for m in provider.requests[0].messages) == 1
        assert list(session.full_history) == [
            row.message for row in await store.load_messages(session.id)
        ]
    finally:
        await agent.close()


@pytest.mark.parametrize("failure", ["connection", "store-unavailable"])
async def test_cancelled_inbox_commit_preserves_cancellation_after_store_error(
    inbox_store: Any, failure: str
) -> None:
    """A late commit failure must not replace the caller's original cancellation."""
    from linch import DurabilityOptions
    from linch.loop.runtime import drain_pending_notifications
    from linch.run_store import _StoreUnavailableError
    from linch.types import Message, TextBlock

    store = _NotificationAppendStore(inbox_store)
    agent = make_agent(
        ScriptedProvider([]),
        session_store=store,
        run_store=None,
        durability=DurabilityOptions(durable_inbox=True),
    )
    try:
        session = await agent.session()
        await session.notify(Message(role="user", content=[TextBlock(text="durable-note")]))
        store.pause = True
        store.commit_error = (
            ConnectionError("commit response failed")
            if failure == "connection"
            else _StoreUnavailableError("commit response unavailable")
        )
        drain = drain_pending_notifications(session, "delivery")
        task = asyncio.create_task(anext(drain))
        await asyncio.wait_for(store.entered.wait(), timeout=1)
        task.cancel("caller cancelled delivery")
        await asyncio.sleep(0)
        task.cancel("caller cancelled again")
        await asyncio.sleep(0)
        store.release.set()
        with pytest.raises(asyncio.CancelledError, match="caller cancelled delivery"):
            await task
        await drain.aclose()
    finally:
        await agent.close()

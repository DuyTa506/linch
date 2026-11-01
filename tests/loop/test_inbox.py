from __future__ import annotations

import asyncio

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn
from tests.support.stores import memory_run_store


class _RunStore:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def append_event(self, _run_id: str, event: object) -> None:
        self.events.append(event)


class _Agent:
    def __init__(self, context: object, store: object) -> None:
        self.context = context
        self.run_store = store


class _Session:
    def __init__(self, agent: _Agent) -> None:
        self.agent = agent
        self.id = "session"
        self._event_journal = None
        self.appended: list[object] = []

    async def append(self, messages: list[object]) -> None:
        self.appended.extend(messages)


class _FailInboxAppendStore:
    def __init__(self) -> None:
        from linch.sessions import InMemorySessionStore

        self._inner = InMemorySessionStore()
        self.failed = False

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def append_messages(self, session_id: str, messages: list[object]) -> object:
        is_inbox = any(
            getattr(block, "text", None) == "inbox-message"
            for message in messages
            for block in getattr(message, "content", ())
        )
        if is_inbox and not self.failed:
            self.failed = True
            raise OSError("inbox append connection lost")
        return await self._inner.append_messages(session_id, messages)


def _new_session() -> tuple[object, _RunStore, _Session]:
    from linch.kernel import Context

    context = Context()
    store = _RunStore()
    return context, store, _Session(_Agent(context, store))


@pytest.mark.asyncio
async def test_inbox_preserves_producer_order_and_message_event_persistence() -> None:
    from linch.events import UserEvent
    from linch.loop.runtime import drain_inbox
    from linch.types import Message, TextBlock

    context, store, session = _new_session()
    closed: list[str] = []

    async def first(_session: object, _run_id: str):
        try:
            yield Message(role="user", content=[TextBlock(text="first")])
            yield UserEvent(
                message=Message(role="user", content=[TextBlock(text="already committed")]),
                subtype="notification",
            )
        finally:
            closed.append("first")

    async def second(_session: object, _run_id: str):
        try:
            yield Message(role="user", content=[TextBlock(text="second")])
        finally:
            closed.append("second")

    context.inbox(first)
    context.inbox(second)
    events = [event async for event in drain_inbox(session, "run")]

    assert [event.message.content[0].text for event in events] == [
        "first",
        "already committed",
        "second",
    ]
    assert [message.content[0].text for message in session.appended] == ["first", "second"]
    assert len(store.events) == 3
    assert closed == ["first", "second"]


@pytest.mark.asyncio
async def test_inbox_closes_iterator_after_producer_failure() -> None:
    from linch.events import UserEvent
    from linch.loop.runtime import drain_inbox
    from linch.types import Message, TextBlock

    context, store, session = _new_session()
    closed = False
    second_called = False

    async def failing(_session: object, _run_id: str):
        nonlocal closed
        try:
            yield Message(role="user", content=[TextBlock(text="before failure")])
            yield UserEvent(
                message=Message(role="user", content=[TextBlock(text="committed first")]),
                subtype="notification",
            )
            raise RuntimeError("producer failed")
        finally:
            closed = True

    async def second(_session: object, _run_id: str):
        nonlocal second_called
        second_called = True
        raise AssertionError("later producer must not run")
        yield  # pragma: no cover

    context.inbox(failing)
    context.inbox(second)
    with pytest.raises(RuntimeError, match="producer failed"):
        _ = [event async for event in drain_inbox(session, "run")]
    assert closed
    assert not second_called
    assert len(store.events) == 2


@pytest.mark.asyncio
async def test_inbox_closes_active_producer_when_consumer_stops() -> None:
    from linch.loop.runtime import drain_inbox
    from linch.types import Message, TextBlock

    context, _, session = _new_session()
    closed = False

    async def held(_session: object, _run_id: str):
        nonlocal closed
        try:
            yield Message(role="user", content=[TextBlock(text="first")])
            await asyncio.Future()
        finally:
            closed = True

    context.inbox(held)
    drainer = drain_inbox(session, "run")
    await drainer.__anext__()
    await drainer.aclose()
    assert closed


@pytest.mark.asyncio
async def test_session_run_closes_inbox_producer_when_consumer_closes() -> None:
    from linch.types import Message, TextBlock

    agent = make_agent(
        ScriptedProvider([]),
        model="m",
        permissions=None,
        run_store=None,
        result_offload=None,
    )
    session = await agent.session()
    closed = False

    async def held(_session: object, _run_id: str):
        nonlocal closed
        try:
            yield Message(role="user", content=[TextBlock(text="first")])
            await asyncio.Future()
        finally:
            closed = True

    agent.context.inbox(held)
    run = session.run("prompt")
    while True:
        event = await run.__anext__()
        if event.type == "user" and event.message.content[0].text == "first":
            break
    await run.aclose()
    assert closed
    await agent.close()


@pytest.mark.asyncio
async def test_inbox_supports_iterators_without_aclose() -> None:
    from linch.loop.runtime import drain_inbox
    from linch.types import Message, TextBlock

    class Iterator:
        def __init__(self) -> None:
            self._done = False

        def __aiter__(self) -> Iterator:
            return self

        async def __anext__(self) -> Message:
            if self._done:
                raise StopAsyncIteration
            self._done = True
            return Message(role="user", content=[TextBlock(text="plain iterator")])

    context, _, session = _new_session()
    context.inbox(lambda _session, _run_id: Iterator())

    events = [event async for event in drain_inbox(session, "run")]
    assert events[0].message.content[0].text == "plain iterator"


@pytest.mark.asyncio
async def test_inbox_append_store_outage_leaves_run_retryable_before_provider() -> None:
    from linch.events import ErrorEvent, ResultEvent, UserEvent
    from linch.session import RunOptions
    from linch.types import Message, TextBlock

    provider = ScriptedProvider([TextTurn(text="provider ran")])
    agent = make_agent(
        provider,
        model="m",
        session_store=_FailInboxAppendStore(),
        run_store=memory_run_store(),
        result_offload=None,
    )
    session = await agent.session(id="inbox-outage")

    async def producer(_session: object, _run_id: str):
        yield Message(role="user", content=[TextBlock(text="inbox-message")])

    agent.context.inbox(producer)
    run_id = "inbox-outage-run"
    first = [event async for event in session.run("prompt", RunOptions(run_id=run_id))]

    assert not any(isinstance(event, (ErrorEvent, ResultEvent)) for event in first)
    assert provider.requests == []
    record = await agent.run_store.load_run(run_id)
    assert record is not None and record.status == "running"

    resumed = [event async for event in session.resume(run_id)]
    assert len(provider.requests) == 1
    assert any(
        isinstance(event, UserEvent) and event.message.content[0].text == "inbox-message"
        for event in resumed
    )
    assert resumed[-1].type == "result" and resumed[-1].subtype == "success"
    await agent.close()

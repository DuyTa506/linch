"""Opt-in bounds preserve busy sessions and durable conversation history."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest


def _agent(**kwargs: Any) -> Any:
    from linch import Agent
    from linch.evals import ScriptedProvider, TextTurn

    kwargs.setdefault("provider", ScriptedProvider([TextTurn(text="done")] * 20))
    return Agent(model="m", **kwargs)


async def _run(session: Any) -> None:
    async for _ in session.run("hello"):
        pass


@pytest.mark.parametrize("bound", [None, 0, 1, 2])
async def test_idle_session_bound_is_opt_in(bound: int | None) -> None:
    agent = _agent(max_idle_sessions=bound)
    for i in range(4):
        await _run(await agent.session(id=f"s{i}"))

    assert len(agent._sessions) == (4 if bound is None else bound)
    await agent.close()


@pytest.mark.parametrize("bound", [-1, 1.5, True])
def test_idle_session_bound_rejects_invalid_values(bound: Any) -> None:
    from linch.errors import ConfigError

    with pytest.raises(ConfigError, match="max_idle_sessions"):
        _agent(max_idle_sessions=bound)


def test_agent_options_preserves_existing_positional_store_arguments() -> None:
    from linch import AgentOptions, InMemoryRunStore, InMemorySessionStore, OpenAIOptions

    session_store = InMemorySessionStore()
    run_store = InMemoryRunStore()
    options = AgentOptions("m", None, OpenAIOptions(), None, None, None, session_store, run_store)

    assert options.session_store is session_store
    assert options.run_store is run_store
    assert options.max_idle_sessions is None


async def test_idle_sessions_evict_in_last_use_order() -> None:
    agent = _agent(max_idle_sessions=2)
    a = await agent.session(id="a")
    await _run(a)
    b = await agent.session(id="b")
    await _run(b)
    assert await agent.session(id="a") is a  # Reattachment refreshes recency.
    c = await agent.session(id="c")
    await _run(c)

    assert set(agent._sessions) == {"a", "c"}
    assert b._closed
    await _run(a)  # Run completion also refreshes recency.
    await _run(await agent.session(id="d"))
    assert set(agent._sessions) == {"a", "d"}
    assert c._closed
    await agent.close()


async def test_reserved_active_run_is_never_evicted() -> None:
    agent = _agent(max_idle_sessions=1)
    busy = await agent.session(id="busy")
    busy.run("reserved but not iterated")
    await _run(await agent.session(id="a"))
    await _run(await agent.session(id="b"))

    assert set(agent._sessions) == {"busy", "b"}
    assert busy._active and not busy._closed
    await agent.close()


@pytest.mark.parametrize("worker", [False, True])
async def test_pending_background_work_is_never_evicted(worker: bool) -> None:
    agent = _agent(max_idle_sessions=1)
    busy = await agent.session(id="busy")
    task = asyncio.create_task(asyncio.Event().wait())
    if worker:
        busy.workers["worker"] = SimpleNamespace(task=task)
    else:
        busy.background_tasks.append(task)
    try:
        await _run(busy)
        await _run(await agent.session(id="a"))
        await _run(await agent.session(id="b"))
        assert set(agent._sessions) == {"busy", "b"}
        assert not task.done() and not busy._closed
    finally:
        await agent.close()
    assert task.cancelled()


@pytest.mark.parametrize("worker", [False, True])
async def test_background_completion_reapplies_the_idle_bound(worker: bool) -> None:
    agent = _agent(max_idle_sessions=1)
    busy = await agent.session(id="busy")
    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait())
    if worker:
        busy.workers["worker"] = SimpleNamespace(task=task)
    else:
        busy.background_tasks.append(task)
    await _run(busy)
    await _run(await agent.session(id="newer"))
    assert set(agent._sessions) == {"busy", "newer"}

    gate.set()
    await task
    if agent._idle_prune_task is not None:
        await agent._idle_prune_task

    assert set(agent._sessions) == {"newer"}
    assert busy._closed
    await agent.close()


@pytest.mark.parametrize("notification", [False, True])
async def test_background_delivery_is_retained_until_the_next_run(notification: bool) -> None:
    from linch.events import BackgroundWorkerEvent
    from linch.types import Message, TextBlock

    agent = _agent(max_idle_sessions=0)
    session = await agent.session(id="background")
    gate = asyncio.Event()

    async def complete() -> None:
        await gate.wait()
        if notification:
            await session.notify(Message(role="user", content=[TextBlock(text="completion")]))
        else:
            session.pending_child_events.append(
                BackgroundWorkerEvent(worker_id="w", display_name="Worker", status="completed")
            )

    task = asyncio.create_task(complete())
    session.background_tasks.append(task)
    await _run(session)
    gate.set()
    await task
    if agent._idle_prune_task is not None:
        await agent._idle_prune_task

    assert not session._closed
    assert await agent.session(id=session.id) is session
    events = [event async for event in session.run("collect completion")]
    if notification:
        assert any(
            event.type == "user" and event.message.content == [TextBlock(text="completion")]
            for event in events
        )
    else:
        assert any(event.type == "background_worker" and event.worker_id == "w" for event in events)
    assert session._closed
    assert not agent._sessions
    await agent.close()


async def test_concurrent_runs_are_protected_until_they_finish() -> None:
    from linch.types import Usage

    started = asyncio.Event()
    gate = asyncio.Event()

    class BlockingProvider:
        id = "blocking"
        calls = 0

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req: Any):
            self.calls += 1
            if self.calls == 2:
                started.set()
            await gate.wait()
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}

    agent = _agent(provider=BlockingProvider(), max_idle_sessions=0)
    a = await agent.session(id="a")
    b = await agent.session(id="b")
    tasks = [asyncio.create_task(_run(session)) for session in (a, b)]
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert set(agent._sessions) == {"a", "b"}
        assert a._active and b._active
        gate.set()
        await asyncio.gather(*tasks)
        assert not agent._sessions
    finally:
        gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await agent.close()


async def test_idle_eviction_preserves_an_active_descendant() -> None:
    agent = _agent(max_idle_sessions=1)
    parent = await agent.session(id="parent")
    child = await agent.session(id="child")
    parent.workers["child"] = SimpleNamespace(child_session_id=child.id, task=None)
    child.run("reserved")

    await _run(await agent.session(id="a"))
    await _run(await agent.session(id="b"))

    assert set(agent._sessions) == {"parent", "child", "b"}
    assert not parent._closed and child._active
    await agent.close()


async def test_evicted_session_reloads_history_and_can_run_again() -> None:
    agent = _agent(max_idle_sessions=1)
    old = await agent.session(id="conversation")
    await _run(old)
    history = old.full_history
    await _run(await agent.session(id="other"))

    reloaded = await agent.session(id="conversation")
    assert reloaded is not old
    assert old._closed
    assert reloaded.full_history == history
    await _run(reloaded)
    assert len(reloaded.full_history) == len(history) + 2
    assert set(agent._sessions) == {"conversation"}
    await agent.close()

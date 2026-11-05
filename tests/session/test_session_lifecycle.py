"""Exercise session release, agent teardown, and lifecycle race behavior."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from tests.support.providers import ScriptedProvider, TextTurn


class _Provider(ScriptedProvider):
    id = "p"

    def __init__(self, order: list[str] | None = None) -> None:
        super().__init__([], context_window=100_000)
        self._order = order

    async def stream(self, req: Any) -> AsyncIterator[dict[str, Any]]:
        async for event in ScriptedProvider([TextTurn("done")]).stream(req):
            yield event

    async def aclose(self) -> None:
        if self._order is not None:
            self._order.append("provider_closed")


class _CloseGateProvider(_Provider):
    """Gate provider teardown so close races can observe the in-flight state."""

    def __init__(self, order: list[str] | None = None) -> None:
        super().__init__(order)
        self.calls = 0
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        self.closed = asyncio.Event()

    async def aclose(self) -> None:
        self.calls += 1
        self.entered.set()
        await self.gate.wait()
        if self._order is not None:
            self._order.append("provider_closed")
        self.closed.set()


def _agent(**kwargs: Any):
    from tests.support.agents import make_agent
    from tests.support.stores import memory_session_store

    provider = kwargs.pop("provider", _Provider())
    kwargs.setdefault("model", "gpt-5")
    kwargs.setdefault("session_store", memory_session_store())
    kwargs.setdefault("run_store", None)
    return make_agent(provider, **kwargs)


async def test_release_rejects_active_run_without_force() -> None:
    from linch.errors import ConfigError

    agent = _agent()
    session = await agent.session(id="s1")
    session._active = True
    try:
        await agent.release_session(session)
        raise AssertionError("expected ConfigError for active session without force")
    except ConfigError:
        pass
    assert "s1" in agent._sessions  # not unregistered on rejection


async def test_force_release_aborts_and_unregisters_active_session() -> None:
    agent = _agent()
    session = await agent.session(id="s1")
    session._active = True
    await agent.release_session(session, force=True)
    assert "s1" not in agent._sessions
    assert session._abort_controller.aborted


async def test_force_release_keeps_generator_teardown_error_retryable() -> None:
    agent = _agent()
    session = await agent.session(id="s1")

    class FailingGenerator:
        calls = 0

        async def aclose(self) -> None:
            self.calls += 1
            if self.calls == 1:
                raise ValueError("teardown failed")

    gen = FailingGenerator()
    session._active = True
    session._active_gen = gen

    with pytest.raises(ValueError, match="teardown failed"):
        await agent.release_session(session, force=True)

    assert not session._closed
    assert "s1" in agent._sessions
    assert agent.session_store is not None

    await agent.release_session(session, force=True)

    assert session._closed
    assert "s1" not in agent._sessions
    assert gen.calls == 2


async def test_async_context_manager_releases_on_exit() -> None:
    agent = _agent()
    session = await agent.session(id="s1")
    async with session as s:
        assert s is session
        assert "s1" in agent._sessions
    assert "s1" not in agent._sessions


async def test_agent_awaits_provider_prepare_once_before_first_run() -> None:
    from linch.filesystem.offload import OffloadConfig

    class PreparingProvider(_Provider):
        def __init__(self) -> None:
            super().__init__()
            self.prepared = 0
            self._ctx = 8_192

        def context_window(self, model: str) -> int:
            return self._ctx

        async def prepare(self) -> None:
            self.prepared += 1
            self._ctx = 40_000  # "discovered" a larger window

    provider = PreparingProvider()
    agent = _agent(
        provider=provider,
        result_offload=OffloadConfig(threshold_fraction=0.5),  # auto (no threshold_tokens)
    )
    # Threshold sized from the pre-prepare window (8192 * 0.5).
    assert agent.result_offload.threshold_tokens == 4_096

    async for _ in agent_run(agent, "s1", "hi"):
        pass
    async for _ in agent_run(agent, "s2", "hi"):
        pass

    assert provider.prepared == 1  # coalesced across runs
    # Offload threshold refreshed from the discovered window (40000 * 0.5).
    assert agent.result_offload.threshold_tokens == 20_000


async def agent_run(agent, sid, prompt):
    session = await agent.session(id=sid)
    async for event in session.run(prompt):
        yield event


async def test_release_foreign_agent_session_raises() -> None:
    from linch.errors import ConfigError

    agent_a = _agent()
    agent_b = _agent()
    session_a = await agent_a.session(id="s1")
    try:
        await agent_b.release_session(session_a)
        raise AssertionError("expected ConfigError releasing a foreign agent's session")
    except ConfigError:
        pass
    # The foreign object's own registration is untouched.
    assert "s1" in agent_a._sessions


async def test_cancelled_agent_close_drains_default_plugin_before_returning() -> None:
    provider = _CloseGateProvider()
    agent = _agent(provider=provider)
    session = await agent.session(id="never-started-close")
    session.run("never consumed")
    closing = asyncio.create_task(agent.close())
    await asyncio.wait_for(provider.entered.wait(), 1)
    closing.cancel()
    provider.gate.set()
    try:
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert agent._closed
        assert agent.context._scope.disposed
        assert agent.context.get("request_catalog") is None
        assert not agent.context.events.has_listeners("tools/resolve")
    finally:
        await agent.close()


@pytest.mark.parametrize("operation", ["append"])
async def test_closed_session_rejects_operations(operation: str) -> None:
    from linch.errors import ConfigError

    agent = _agent()
    session = await agent.session(id="s1")
    await session.aclose()
    with pytest.raises(ConfigError):
        if operation == "run":
            session.run("hi")
        else:
            from linch.types import Message, TextBlock

            await session.append([Message(role="user", content=[TextBlock(text="x")])])


async def test_session_setup_rolls_back_when_close_starts() -> None:
    from linch.errors import ConfigError
    from linch.sessions import InMemorySessionStore

    class SlowCreateStore(InMemorySessionStore):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.gate = asyncio.Event()
            self.closed = False

        async def create(self, **kwargs: Any):
            self.entered.set()
            await self.gate.wait()
            return await super().create(**kwargs)

        async def close(self) -> None:
            self.closed = True
            await super().close()

    store = SlowCreateStore()
    agent = _agent(session_store=store)
    create_task = asyncio.create_task(agent.session(id="racing"))
    await asyncio.wait_for(store.entered.wait(), 1.0)

    close_task = asyncio.create_task(agent.close())
    await asyncio.sleep(0)
    assert agent._lifecycle_state == "quiescing"
    assert not store.closed

    store.gate.set()
    with pytest.raises(ConfigError, match="agent is closing"):
        await create_task
    await close_task

    assert agent._lifecycle_state == "closed"
    assert agent._sessions == {}
    assert store.closed


async def test_concurrent_agent_close_callers_share_one_teardown() -> None:
    provider = _CloseGateProvider()
    agent = _agent(provider=provider)
    first = asyncio.create_task(agent.close())
    second = asyncio.create_task(agent.close())
    await asyncio.wait_for(provider.entered.wait(), 1.0)

    assert provider.calls == 1
    assert not first.done()
    assert not second.done()
    provider.gate.set()
    await asyncio.gather(first, second)

    assert provider.calls == 1
    assert agent._lifecycle_state == "closed"


async def test_agent_close_failure_retries_only_unfinished_resources() -> None:
    from linch.sessions import InMemorySessionStore

    class CountingStore(InMemorySessionStore):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            await super().close()

    class FailOnceProvider(_Provider):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("provider teardown failed")

    store = CountingStore()
    provider = FailOnceProvider()
    agent = _agent(provider=provider, session_store=store)
    session = await agent.session(id="s1")

    with pytest.raises(RuntimeError, match="provider teardown failed"):
        await agent.close()

    assert agent._lifecycle_state == "quiescing"
    assert session._closed
    assert store.close_calls == 1
    assert provider.close_calls == 1

    await agent.close()

    assert agent._lifecycle_state == "closed"
    assert store.close_calls == 1
    assert provider.close_calls == 2


async def test_new_admission_is_rejected_while_agent_quiesces() -> None:
    from linch.errors import ConfigError

    provider = _CloseGateProvider()
    agent = _agent(provider=provider)
    session = await agent.session(id="existing")
    close_task = asyncio.create_task(agent.close())
    await asyncio.wait_for(provider.entered.wait(), 1.0)

    with pytest.raises(ConfigError, match="agent is closing"):
        await agent.session(id="new")
    with pytest.raises(ConfigError):
        session.run("late")

    provider.gate.set()
    await close_task


async def test_profile_subagent_close_race_uses_one_agent_lifecycle() -> None:
    from linch import Profile, RunOptions
    from linch.config import FeatureFlags
    from tests.support.agents import make_agent
    from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
    from tests.support.tools import tool_registry

    started, release = asyncio.Event(), asyncio.Event()

    class HeldChild(ScriptedProvider):
        async def stream(self, req):
            if self.requests:
                started.set()
                await release.wait()
            async for event in super().stream(req):
                yield event

    provider = HeldChild(
        [
            ToolUseTurn("Subagent", {"description": "child", "prompt": "child"}),
            TextTurn("child"),
            TextTurn("done"),
        ]
    )
    agent = make_agent(
        provider,
        tools=tool_registry(),
        run_store=None,
        max_idle_sessions=10,
        features=FeatureFlags(subagents=True),
    )
    parent = await agent.session()

    async def consume():
        return [
            event async for event in parent.run("go", RunOptions(profile=Profile("empty", "1")))
        ]

    running = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 1)
    closing = asyncio.create_task(agent.close())
    await asyncio.sleep(0)
    release.set()
    try:
        await asyncio.wait_for(asyncio.gather(running, closing), 1)
    finally:
        running.cancel()
        closing.cancel()
    assert parent._closed
    assert agent._sessions == {}

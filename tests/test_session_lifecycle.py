"""Session lifecycle: public release/aclose API and drain-before-close ordering.

Covers Phase 1.3 (finalizers run before resources close) and Phase 2.1 (public
per-session release, idempotency, active-run rejection, forced drain, retained
child recursion, and registry churn returning to baseline).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any


class _Provider:
    id = "p"

    def __init__(self, order: list[str] | None = None) -> None:
        self._order = order

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}

    async def aclose(self) -> None:
        if self._order is not None:
            self._order.append("provider_closed")


def _agent(**kwargs: Any):
    from linch import Agent
    from linch.sessions import InMemorySessionStore

    kwargs.setdefault("model", "gpt-5")
    kwargs.setdefault("provider", _Provider())
    kwargs.setdefault("session_store", InMemorySessionStore())
    kwargs.setdefault("cwd", ".")
    return Agent(**kwargs)


async def test_release_unknown_session_is_noop() -> None:
    agent = _agent()
    await agent.release_session("does-not-exist")  # no raise
    await agent.release_session("does-not-exist", force=True)  # still no raise


async def test_release_is_idempotent() -> None:
    agent = _agent()
    session = await agent.session(id="s1")
    assert "s1" in agent._sessions
    await agent.release_session(session)
    assert "s1" not in agent._sessions
    # Second release (by id and by object) is a no-op.
    await agent.release_session("s1")
    await session.aclose()
    assert "s1" not in agent._sessions


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


async def test_async_context_manager_releases_on_exit() -> None:
    agent = _agent()
    session = await agent.session(id="s1")
    async with session as s:
        assert s is session
        assert "s1" in agent._sessions
    assert "s1" not in agent._sessions


async def test_force_release_drains_owned_background_task() -> None:
    agent = _agent()
    session = await agent.session(id="s1")
    finalized = asyncio.Event()

    async def bg() -> None:
        try:
            await asyncio.sleep(100)
        finally:
            finalized.set()

    task = asyncio.create_task(bg())
    await asyncio.sleep(0)  # let it start
    session.background_tasks.append(task)

    await agent.release_session(session, force=True)

    assert task.done()
    assert finalized.is_set()
    assert session.background_tasks == []


async def test_agent_close_runs_task_finalizers_before_closing_provider() -> None:
    order: list[str] = []
    agent = _agent(provider=_Provider(order))
    session = await agent.session(id="s1")

    async def bg() -> None:
        try:
            await asyncio.sleep(100)
        finally:
            order.append("task_finalized")

    task = asyncio.create_task(bg())
    await asyncio.sleep(0)
    session.background_tasks.append(task)

    await agent.close()

    assert order == ["task_finalized", "provider_closed"], order


async def test_force_release_recurses_into_retained_child_session() -> None:
    from linch.subagents.types import AgentDefinition, AgentFrontmatter
    from linch.subagents.workers import WorkerHandle

    agent = _agent()
    parent = await agent.session(id="parent")
    await agent.session(id="child")
    assert {"parent", "child"} <= set(agent._sessions)

    definition = AgentDefinition(
        name="w",
        file_path="<test>",
        source="built-in",
        frontmatter=AgentFrontmatter(name="w", description="d"),
        body="",
    )
    parent.workers["agent-1"] = WorkerHandle(
        worker_id="agent-1",
        child_session_id="child",
        display_name="Worker",
        definition=definition,
        status="running",
        task=None,
    )

    await agent.release_session("parent", force=True)

    # Both the parent and its retained child are unregistered.
    assert "parent" not in agent._sessions
    assert "child" not in agent._sessions


async def test_session_churn_returns_registry_and_tasks_to_baseline() -> None:
    agent = _agent()
    baseline_sessions = len(agent._sessions)
    baseline_tasks = len(asyncio.all_tasks())

    for i in range(500):
        session = await agent.session(id=f"s{i}")
        async for _ in session.run("hi"):
            pass
        await agent.release_session(session)

    assert len(agent._sessions) == baseline_sessions
    # No owned background tasks leaked across the churn.
    assert len(asyncio.all_tasks()) <= baseline_tasks


# ── Phase 1.4: provider.prepare() is coalesced and awaited before the first run ──


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


# ── Ownership & lifecycle hardening ──


async def test_session_same_id_returns_same_live_instance() -> None:
    agent = _agent()
    first = await agent.session(id="dup")
    second = await agent.session(id="dup")
    # Re-attaching a live id returns the registered instance instead of
    # overwriting it (which would orphan the first Session and its work).
    assert second is first
    assert agent._sessions["dup"] is first


async def test_agent_close_is_idempotent() -> None:
    order: list[str] = []
    agent = _agent(provider=_Provider(order))
    await agent.session(id="s1")
    await agent.close()
    await agent.close()  # second call must be a no-op
    assert order.count("provider_closed") == 1


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


async def test_release_stale_instance_does_not_unregister_replacement() -> None:
    agent = _agent()
    stale = await agent.session(id="x")
    await agent.release_session(stale)  # unregisters the stale instance
    fresh = await agent.session(id="x")  # a new live instance under the same id
    assert agent._sessions["x"] is fresh
    # Releasing the now-stale object must not disturb the fresh registration.
    await agent.release_session(stale)
    assert agent._sessions.get("x") is fresh
    assert not fresh._closed


async def test_provider_reassignment_reruns_prepare() -> None:
    class _PreparingProvider(_Provider):
        def __init__(self) -> None:
            super().__init__()
            self.prepared = 0

        async def prepare(self) -> None:
            self.prepared += 1

    p1 = _PreparingProvider()
    p2 = _PreparingProvider()
    agent = _agent(provider=p1)
    async for _ in agent_run(agent, "s1", "hi"):
        pass
    assert p1.prepared == 1
    # Reassigning the provider must reset prepare-state so the new provider's
    # one-time prepare() actually runs before the next run.
    agent.provider = p2
    async for _ in agent_run(agent, "s2", "hi"):
        pass
    assert p2.prepared == 1


async def test_close_cancelled_mid_teardown_still_releases_provider() -> None:
    import pytest

    order: list[str] = []

    class _SlowCloseProvider(_Provider):
        def __init__(self, order: list[str]) -> None:
            super().__init__(order)
            self.entered = asyncio.Event()
            self.gate = asyncio.Event()
            self.closed = asyncio.Event()

        async def aclose(self) -> None:
            self.entered.set()
            await self.gate.wait()
            assert self._order is not None
            self._order.append("provider_closed")
            self.closed.set()

    provider = _SlowCloseProvider(order)
    agent = _agent(provider=provider)
    await agent.session(id="s1")

    close_task = asyncio.create_task(agent.close())
    await asyncio.wait_for(provider.entered.wait(), 1.0)  # teardown reached provider
    close_task.cancel()  # cancel mid-teardown
    provider.gate.set()  # let the shielded teardown finish
    with pytest.raises(asyncio.CancelledError):
        await close_task
    # The shielded teardown still released the provider despite the cancellation.
    await asyncio.wait_for(provider.closed.wait(), 1.0)
    assert order.count("provider_closed") == 1


async def test_run_after_close_raises() -> None:
    from linch.errors import ConfigError

    agent = _agent()
    session = await agent.session(id="s1")
    await session.aclose()
    try:
        session.run("hi")
        raise AssertionError("expected ConfigError running a closed session")
    except ConfigError:
        pass


async def test_append_after_close_raises() -> None:
    from linch.errors import ConfigError
    from linch.types import Message, TextBlock

    agent = _agent()
    session = await agent.session(id="s1")
    await session.aclose()
    try:
        await session.append([Message(role="user", content=[TextBlock(text="x")])])
        raise AssertionError("expected ConfigError appending to a closed session")
    except ConfigError:
        pass
